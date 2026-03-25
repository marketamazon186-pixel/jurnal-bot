import os
import re
import logging
from datetime import datetime
from psycopg2 import pool
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG & ENV ──────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
DB_URL           = os.environ.get("DATABASE_URL", "")
CREDENTIALS_FILE = "credentials.json"
SERVICE_EMAIL    = "jurnal-bot-2@fresh-gravity-488918-f1.iam.gserviceaccount.com"
TEMPLATE_LINK    = "https://docs.google.com/spreadsheets/d/GANTI_DENGAN_ID_TEMPLATE_ANDA/edit"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── DATABASE CONNECTION POOL ──────────────────────────────────────────────────
try:
    db_pool = pool.SimpleConnectionPool(1, 20, DB_URL)
    if not db_pool:
        raise ValueError("Pool gagal diinisialisasi.")
except Exception as e:
    logger.critical(f"DB Connection Error: {e}")
    raise SystemExit(1)

def execute_query(query: str, params: tuple = None, fetchone: bool = False, fetchall: bool = False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetchone:
                result = cur.fetchone()
                conn.commit()
                return result
            if fetchall:
                result = cur.fetchall()
                conn.commit()
                return result
            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"SQL Execution Error: {e}")
        raise
    finally:
        db_pool.putconn(conn)

def get_user(chat_id: str):
    query = "SELECT spreadsheet_id, spreadsheet_link FROM users WHERE chat_id = %s AND status = 'aktif';"
    row = execute_query(query, (chat_id,), fetchone=True)
    if row:
        return {"spreadsheet_id": row[0], "link": row[1]}
    return None

def upsert_user(chat_id: str, nama: str, spreadsheet_id: str, link: str):
    query = """
        INSERT INTO users (chat_id, nama, spreadsheet_id, spreadsheet_link)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE 
        SET spreadsheet_id = EXCLUDED.spreadsheet_id, 
            spreadsheet_link = EXCLUDED.spreadsheet_link,
            status = 'aktif';
    """
    execute_query(query, (chat_id, nama, spreadsheet_id, link))

# ── GOOGLE SHEETS INTEGRATION ─────────────────────────────────────────────────
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds)

def verify_and_init_sheet(spreadsheet_id: str) -> bool:
    try:
        client = get_gspread_client()
        ss = client.open_by_key(spreadsheet_id)
        try:
            ss.worksheet("Transaksi")
        except gspread.WorksheetNotFound:
            sheet = ss.add_worksheet(title="Transaksi", rows=1000, cols=10)
            sheet.append_row(["Tanggal", "Waktu", "Tipe", "Kategori", "Jumlah", "Catatan"])
        return True
    except Exception as e:
        logger.error(f"Gspread Verification Error: {e}")
        return False

def add_transaction(spreadsheet_id: str, tipe: str, kategori: str, jumlah: int, catatan: str):
    client = get_gspread_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet("Transaksi")
    now = datetime.now()
    sheet.append_row([
        now.strftime("%d/%m/%Y"),
        now.strftime("%H:%M:%S"),
        tipe.upper(),
        kategori,
        jumlah,
        catatan
    ])

def get_summary(spreadsheet_id: str, period: str = "hari") -> str:
    client = get_gspread_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet("Transaksi")
    records = sheet.get_all_records()
    now = datetime.now()
    pemasukan, pengeluaran = 0, 0
    detail_masuk, detail_keluar = [], []

    for row in records:
        try:
            tgl = datetime.strptime(row.get("Tanggal", ""), "%d/%m/%Y")
        except ValueError:
            continue
            
        if period == "hari" and tgl.date() != now.date():
            continue
        if period == "bulan" and (tgl.month != now.month or tgl.year != now.year):
            continue

        try:
            jumlah = int(str(row.get("Jumlah", 0)).replace(".", "").replace(",", ""))
        except ValueError:
            continue

        tipe = str(row.get("Tipe", "")).upper()
        kategori = str(row.get("Kategori", "-"))
        catatan = str(row.get("Catatan", ""))

        format_item = f"  • {kategori}: Rp {jumlah:,}".replace(",", ".") + (f" ({catatan})" if catatan else "")
        if tipe == "MASUK":
            pemasukan += jumlah
            detail_masuk.append(format_item)
        elif tipe == "KELUAR":
            pengeluaran += jumlah
            detail_keluar.append(format_item)

    saldo = pemasukan - pengeluaran
    label = "Hari Ini" if period == "hari" else f"Bulan {now.strftime('%B %Y')}"
    
    text = f"📊 *Laporan {label}*\n\n"
    text += "💚 *Pemasukan:*\n" + ("\n".join(detail_masuk) if detail_masuk else "Belum ada") + f"\n*Total: Rp {pemasukan:,}*\n\n".replace(",", ".")
    text += "🔴 *Pengeluaran:*\n" + ("\n".join(detail_keluar) if detail_keluar else "Belum ada") + f"\n*Total: Rp {pengeluaran:,}*\n\n".replace(",", ".")
    text += f"{'✅' if saldo >= 0 else '⚠️'} *Saldo: Rp {saldo:,}*".replace(",", ".")
    return text

# ── LOGIC PARSER ──────────────────────────────────────────────────────────────
def parse_transaksi(text: str):
    match = re.match(r'^(masuk|\+|keluar|-)\s+([\d.,]+)\s*(.*)$', text.strip().lower())
    if not match:
        return None
    tipe_raw, jumlah_raw, sisa = match.groups()
    tipe = "MASUK" if tipe_raw in ("masuk", "+") else "KELUAR"
    jumlah = int(re.sub(r'[.,]', '', jumlah_raw))
    parts = sisa.strip().split(" ", 1)
    kategori = parts[0].capitalize() if parts[0] else "Lainnya"
    catatan = parts[1] if len(parts) > 1 else ""
    return tipe, kategori, jumlah, catatan

# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = get_user(chat_id)
    
    keyboard = [
        ["📥 Catat Masuk", "📤 Catat Keluar"],
        ["📊 Laporan Hari Ini", "📅 Laporan Bulan Ini"],
        ["❓ Bantuan"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if user:
        await update.message.reply_text(f"✅ Sistem aktif. Terhubung ke:\n{user['link']}", reply_markup=markup)
    else:
        await update.message.reply_text("Sistem Jurnal Aktif. Ketik /daftar untuk inisiasi.", reply_markup=markup)

async def daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instruksi = (
        "⚙️ *Prosedur Registrasi:*\n\n"
        f"1. Buka Template: [Klik Disini]({TEMPLATE_LINK})\n"
        "2. Pilih menu *File* > *Make a copy*.\n"
        f"3. Klik *Share*, ubah akses ke *Editor* untuk email berikut:\n`{SERVICE_EMAIL}`\n"
        "4. Copy link spreadsheet kamu yang baru.\n"
        "5. Kirim ke bot dengan format:\n`/setlink [LINK_SPREADSHEET]`"
    )
    await update.message.reply_text(instruksi, parse_mode="Markdown", disable_web_page_preview=True)

async def setlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    nama = update.effective_user.first_name or "User"
    pesan = update.message.text.replace("/setlink", "").strip()
    
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", pesan)
    if not match:
        await update.message.reply_text("❌ Format URL tidak valid. Ekstraksi ID gagal.")
        return
        
    spreadsheet_id = match.group(1)
    await update.message.reply_text("⏳ Memvalidasi akses API...")
    
    if verify_and_init_sheet(spreadsheet_id):
        upsert_user(chat_id, nama, spreadsheet_id, pesan)
        await update.message.reply_text("✅ Autentikasi sukses. Sistem siap menerima transaksi.")
    else:
        await update.message.reply_text(f"❌ Akses ditolak. Pastikan email `{SERVICE_EMAIL}` telah diberikan izin *Editor*.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    chat_id = str(update.effective_chat.id)
    
    if text in ["📊 laporan hari ini", "laporan hari"]:
        user = get_user(chat_id)
        if not user: return await update.message.reply_text("❌ Akses ditolak. Eksekusi /daftar.")
        await update.message.reply_text(get_summary(user["spreadsheet_id"], "hari"), parse_mode="Markdown")
        return

    if text in ["📅 laporan bulan ini", "laporan bulan"]:
        user = get_user(chat_id)
        if not user: return await update.message.reply_text("❌ Akses ditolak. Eksekusi /daftar.")
        await update.message.reply_text(get_summary(user["spreadsheet_id"], "bulan"), parse_mode="Markdown")
        return

    result = parse_transaksi(text)
    if result:
        user = get_user(chat_id)
        if not user:
            await update.message.reply_text("❌ Akses ditolak. Eksekusi /daftar.")
            return
            
        tipe, kategori, jumlah, catatan = result
        try:
            add_transaction(user["spreadsheet_id"], tipe, kategori, jumlah, catatan)
            await update.message.reply_text(f"✅ Input diotorisasi:\n[{tipe}] {kategori} - Rp {jumlah:,}".replace(",", "."))
        except Exception as e:
            logger.error(f"Failed transaction injection: {e}")
            await update.message.reply_text("❌ Celah I/O. Gagal menulis ke Spreadsheet.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("daftar", daftar))
    app.add_handler(CommandHandler("setlink", setlink))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Service initialized.")
    app.run_polling()

if __name__ == "__main__":
    main()
