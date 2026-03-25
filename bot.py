import logging
import os
import re
import requests as req
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
import google.auth.transport.requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
TEMPLATE_ID      = os.environ.get("SPREADSHEET_ID", "")
MASTER_ID        = os.environ.get("MASTER_SPREADSHEET_ID", "")
CREDENTIALS_FILE = "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

_user_cache = {}

# ── GOOGLE AUTH ───────────────────────────────────────────────────────────────
def get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def get_token():
    """Ambil access token dari service account."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

# ── MASTER SHEET ──────────────────────────────────────────────────────────────
def get_master_sheet():
    client = get_client()
    spreadsheet = client.open_by_key(MASTER_ID)
    try:
        sheet = spreadsheet.worksheet("Users")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Users", rows=1000, cols=6)
        sheet.append_row(["chat_id", "nama", "spreadsheet_id", "link", "tanggal_daftar", "status"])
    return sheet

def cari_user(chat_id: str):
    if chat_id in _user_cache:
        return _user_cache[chat_id]
    try:
        sheet = get_master_sheet()
        records = sheet.get_all_records()
        for row in records:
            if str(row.get("chat_id", "")) == str(chat_id):
                _user_cache[chat_id] = row
                return row
    except Exception as e:
        logger.error(f"Error cari_user: {e}")
    return None

def daftarkan_user(chat_id: str, nama: str):
    """Duplikasi template spreadsheet untuk user baru."""
    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 1. Copy template ke Drive milik Service Account (bukan Drive pribadi)
    copy_title = f"Jurnal Keuangan - {nama}"
    resp = req.post(
        f"https://www.googleapis.com/drive/v3/files/{TEMPLATE_ID}/copy",
        headers=headers,
        params={"supportsAllDrives": "true"},
        json={
            "name": copy_title,
            "parents": ["root"]  # Simpan ke root Drive Service Account
        }
    )
    if resp.status_code != 200:
        raise Exception(f"Gagal copy template: {resp.text}")

    new_id = resp.json()["id"]
    link = f"https://docs.google.com/spreadsheets/d/{new_id}/edit"

    # 2. Share ke anyone (writer) agar user bisa akses
    req.post(
        f"https://www.googleapis.com/drive/v3/files/{new_id}/permissions",
        headers=headers,
        params={"supportsAllDrives": "true"},
        json={"role": "writer", "type": "anyone"}
    )

    # 3. Update Chat ID di sheet Setting
    try:
        client = get_client()
        new_ss = client.open_by_key(new_id)
        setting_sheet = new_ss.worksheet("⚙️ Setting")
        setting_sheet.update("B5", str(chat_id))
    except Exception as e:
        logger.warning(f"Tidak bisa update setting sheet: {e}")

    # 4. Simpan ke master
    sheet = get_master_sheet()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    sheet.append_row([chat_id, nama, new_id, link, now, "aktif"])

    # 5. Cache
    _user_cache[chat_id] = {
        "chat_id": chat_id,
        "nama": nama,
        "spreadsheet_id": new_id,
        "link": link,
        "tanggal_daftar": now,
        "status": "aktif"
    }

    return new_id, link

# ── USER SHEET ────────────────────────────────────────────────────────────────
def get_user_sheet(chat_id: str):
    user = cari_user(str(chat_id))
    if not user:
        raise ValueError("BELUM_DAFTAR")
    client = get_client()
    spreadsheet = client.open_by_key(user["spreadsheet_id"])
    try:
        sheet = spreadsheet.worksheet("Transaksi")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Transaksi", rows=1000, cols=10)
        sheet.append_row(["Tanggal", "Waktu", "Tipe", "Kategori", "Jumlah", "Catatan"])
    return sheet

def add_transaction(chat_id: str, tipe, kategori, jumlah, catatan=""):
    sheet = get_user_sheet(chat_id)
    now = datetime.now()
    sheet.append_row([
        now.strftime("%d/%m/%Y"),
        now.strftime("%H:%M:%S"),
        tipe.upper(),
        kategori,
        jumlah,
        catatan
    ])

def get_summary(chat_id: str, period="hari"):
    sheet = get_user_sheet(chat_id)
    records = sheet.get_all_records()
    now = datetime.now()
    pemasukan = 0
    pengeluaran = 0
    detail_masuk = []
    detail_keluar = []

    for row in records:
        try:
            tgl = datetime.strptime(row.get("Tanggal", ""), "%d/%m/%Y")
        except:
            continue
        if period == "hari" and tgl.date() != now.date():
            continue
        elif period == "bulan" and (tgl.month != now.month or tgl.year != now.year):
            continue

        jumlah = int(str(row.get("Jumlah", 0)).replace(".", "").replace(",", ""))
        tipe = str(row.get("Tipe", "")).upper()
        kategori = row.get("Kategori", "-")
        catatan = row.get("Catatan", "")

        if tipe == "MASUK":
            pemasukan += jumlah
            detail_masuk.append(f"  + {kategori}: Rp {jumlah:,}".replace(",", ".") + (f" ({catatan})" if catatan else ""))
        elif tipe == "KELUAR":
            pengeluaran += jumlah
            detail_keluar.append(f"  - {kategori}: Rp {jumlah:,}".replace(",", ".") + (f" ({catatan})" if catatan else ""))

    saldo = pemasukan - pengeluaran
    periode_label = "Hari Ini" if period == "hari" else f"Bulan {now.strftime('%B %Y')}"
    text = f"📊 *Laporan {periode_label}*\n\n"

    if detail_masuk:
        text += "💚 *Pemasukan:*\n" + "\n".join(detail_masuk) + "\n"
        text += f"*Total Masuk: Rp {pemasukan:,}*\n\n".replace(",", ".")
    else:
        text += "💚 *Pemasukan:* Belum ada\n\n"

    if detail_keluar:
        text += "🔴 *Pengeluaran:*\n" + "\n".join(detail_keluar) + "\n"
        text += f"*Total Keluar: Rp {pengeluaran:,}*\n\n".replace(",", ".")
    else:
        text += "🔴 *Pengeluaran:* Belum ada\n\n"

    emoji_saldo = "✅" if saldo >= 0 else "⚠️"
    text += f"{emoji_saldo} *Saldo: Rp {saldo:,}*".replace(",", ".")
    return text

# ── PARSE TRANSAKSI ───────────────────────────────────────────────────────────
def parse_transaksi(text):
    text = text.strip().lower()
    pattern = r'^(masuk|\+|keluar|-)\s+([\d.,]+)\s*(.*)$'
    match = re.match(pattern, text)
    if not match:
        return None
    tipe_raw, jumlah_raw, sisanya = match.groups()
    tipe = "MASUK" if tipe_raw in ("masuk", "+") else "KELUAR"
    jumlah = int(jumlah_raw.replace(".", "").replace(",", ""))
    parts = sisanya.strip().split(" ", 1)
    kategori = parts[0].capitalize() if parts[0] else "Lainnya"
    catatan = parts[1] if len(parts) > 1 else ""
    return tipe, kategori, jumlah, catatan

# ── HANDLERS ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = cari_user(chat_id)
    keyboard = [
        ["📥 Catat Masuk", "📤 Catat Keluar"],
        ["📊 Laporan Hari Ini", "📅 Laporan Bulan Ini"],
        ["❓ Bantuan"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    if user:
        await update.message.reply_text(
            f"👋 Halo kembali!\n\n"
            f"📊 Spreadsheet kamu:\n{user['link']}\n\n"
            f"Ketik *Bantuan* untuk panduan.",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "👋 Halo! Selamat datang di *Jurnal Keuangan Bot*! 💰\n\n"
            "Bot ini membantu kamu mencatat keuangan otomatis ke Google Spreadsheet!\n\n"
            "Untuk mulai, ketik:\n👉 /daftar\n\n"
            "Spreadsheet pribadi kamu akan langsung dibuat secara otomatis!",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

async def daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    nama = update.effective_user.first_name or "User"

    existing = cari_user(chat_id)
    if existing:
        await update.message.reply_text(
            f"✅ Kamu sudah terdaftar!\n\n"
            f"📊 Spreadsheet kamu:\n{existing['link']}\n\n"
            f"Mulai catat: `masuk 50000 Gaji`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "⏳ Sedang menyiapkan spreadsheet pribadimu...\nMohon tunggu sebentar ya! 🙏"
    )

    try:
        spreadsheet_id, link = daftarkan_user(chat_id, nama)
        await update.message.reply_text(
            f"🎉 *Selamat {nama}! Pendaftaran berhasil!*\n\n"
            f"📊 Spreadsheet pribadimu sudah siap:\n"
            f"👉 {link}\n\n"
            f"*Cara catat transaksi:*\n"
            f"`masuk 500000 Gaji`\n"
            f"`keluar 25000 Makan siang`\n\n"
            f"Ketik *Bantuan* untuk panduan lengkap. 💪",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error daftar: {e}")
        await update.message.reply_text(
            "❌ Maaf, ada gangguan saat mendaftar.\n"
            "Silakan coba lagi dalam beberapa menit ya!"
        )

async def bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Panduan Jurnal Keuangan Bot*\n\n"
        "*🆕 Daftar (pertama kali):*\n`/daftar`\n\n"
        "*💰 Catat Pemasukan:*\n"
        "`masuk 50000 Gaji`\n`+ 100000 Transfer`\n\n"
        "*💸 Catat Pengeluaran:*\n"
        "`keluar 25000 Makan siang`\n`- 50000 Bensin`\n\n"
        "*📊 Laporan:*\n"
        "Ketik `laporan hari` atau `laporan bulan`\n\n"
        "*📋 Lihat spreadsheet:*\n`/spreadsheet`",
        parse_mode="Markdown"
    )

async def spreadsheet_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = cari_user(chat_id)
    if user:
        await update.message.reply_text(
            f"📊 *Spreadsheet kamu:*\n{user['link']}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Kamu belum daftar! Ketik /daftar dulu. 😊")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    text_lower = text.lower()
    chat_id = str(update.effective_chat.id)

    if text_lower in ["📊 laporan hari ini", "laporan hari"]:
        try:
            await update.message.reply_text(get_summary(chat_id, "hari"), parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Kamu belum daftar! Ketik /daftar dulu. 😊")
        return

    if text_lower in ["📅 laporan bulan ini", "laporan bulan"]:
        try:
            await update.message.reply_text(get_summary(chat_id, "bulan"), parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Kamu belum daftar! Ketik /daftar dulu. 😊")
        return

    if text_lower in ["❓ bantuan", "bantuan", "help"]:
        await bantuan(update, context)
        return

    if text_lower == "📥 catat masuk":
        await update.message.reply_text(
            "💚 Kirim pemasukan:\n`masuk [jumlah] [kategori]`\n\nContoh: `masuk 500000 Gaji`",
            parse_mode="Markdown"
        )
        return

    if text_lower == "📤 catat keluar":
        await update.message.reply_text(
            "🔴 Kirim pengeluaran:\n`keluar [jumlah] [kategori]`\n\nContoh: `keluar 25000 Makan`",
            parse_mode="Markdown"
        )
        return

    result = parse_transaksi(text)
    if result:
        tipe, kategori, jumlah, catatan = result
        try:
            add_transaction(chat_id, tipe, kategori, jumlah, catatan)
            emoji = "💚" if tipe == "MASUK" else "🔴"
            label = "Pemasukan" if tipe == "MASUK" else "Pengeluaran"
            await update.message.reply_text(
                f"{emoji} *{label} dicatat!*\n\n"
                f"Kategori: {kategori}\n"
                f"Jumlah: Rp {jumlah:,}\n".replace(",", ".") +
                (f"Catatan: {catatan}\n" if catatan else "") +
                f"\n✅ Tersimpan di spreadsheet.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Kamu belum daftar!\n\nKetik /daftar untuk membuat spreadsheet pribadimu. 😊"
            )
        except Exception as e:
            logger.error(f"Error saving: {e}")
            await update.message.reply_text("❌ Gagal menyimpan. Coba lagi ya.")
    else:
        await update.message.reply_text(
            "🤔 Format tidak dikenali.\n\n"
            "Contoh:\n`masuk 50000 Gaji`\n`keluar 25000 Makan`\n\n"
            "Ketik *Bantuan* untuk panduan.",
            parse_mode="Markdown"
        )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("daftar", daftar))
    app.add_handler(CommandHandler("bantuan", bantuan))
    app.add_handler(CommandHandler("spreadsheet", spreadsheet_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot multi-user started!")
    app.run_polling()

if __name__ == "__main__":
    main()
