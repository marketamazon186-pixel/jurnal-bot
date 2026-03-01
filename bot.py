import logging
import os
import re
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CREDENTIALS_FILE = "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── GOOGLE SHEETS ────────────────────────────────────────────────────────────
def get_sheet():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        sheet = spreadsheet.worksheet("Transaksi")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Transaksi", rows=1000, cols=10)
        sheet.append_row(["Tanggal", "Waktu", "Tipe", "Kategori", "Jumlah", "Catatan"])
    return sheet

def add_transaction(tipe, kategori, jumlah, catatan=""):
    sheet = get_sheet()
    now = datetime.now()
    tanggal = now.strftime("%d/%m/%Y")
    waktu = now.strftime("%H:%M:%S")
    sheet.append_row([tanggal, waktu, tipe.upper(), kategori, jumlah, catatan])

def get_summary(period="hari"):
    sheet = get_sheet()
    records = sheet.get_all_records()
    now = datetime.now()

    pemasukan = 0
    pengeluaran = 0
    detail_masuk = []
    detail_keluar = []

    for row in records:
        try:
            tgl_str = row.get("Tanggal", "")
            tgl = datetime.strptime(tgl_str, "%d/%m/%Y")
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

# ── PARSE PESAN ──────────────────────────────────────────────────────────────
def parse_transaksi(text):
    """
    Format yang didukung:
    masuk 50000 gaji
    keluar 20000 makan siang
    + 50000 transfer
    - 15000 bensin
    """
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

# ── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["📥 Catat Masuk", "📤 Catat Keluar"],
        ["📊 Laporan Hari Ini", "📅 Laporan Bulan Ini"],
        ["❓ Bantuan"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 Halo! Saya *Jurnal Keuangan Bot* kamu.\n\n"
        "Saya bisa bantu catat pemasukan & pengeluaran kamu langsung ke Google Spreadsheet!\n\n"
        "Ketik *Bantuan* untuk lihat cara pakai.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Cara Pakai:*\n\n"
        "*Catat Pemasukan:*\n"
        "`masuk 50000 Gaji`\n"
        "`+ 100000 Transfer`\n\n"
        "*Catat Pengeluaran:*\n"
        "`keluar 25000 Makan siang`\n"
        "`- 50000 Bensin`\n\n"
        "*Format:* `[masuk/keluar/+/-] [jumlah] [kategori] [catatan opsional]`\n\n"
        "*Laporan:*\n"
        "Ketik `laporan hari` atau `laporan bulan`\n\n"
        "💡 *Tips:* Kamu juga bisa klik tombol menu di bawah!",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    text_lower = text.lower()

    # Tombol menu
    if text_lower in ["📊 laporan hari ini", "laporan hari"]:
        msg = get_summary("hari")
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if text_lower in ["📅 laporan bulan ini", "laporan bulan"]:
        msg = get_summary("bulan")
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if text_lower in ["❓ bantuan", "bantuan", "help"]:
        await bantuan(update, context)
        return

    if text_lower in ["📥 catat masuk"]:
        await update.message.reply_text(
            "💚 Kirim pemasukan dengan format:\n`masuk [jumlah] [kategori]`\n\nContoh: `masuk 500000 Gaji`",
            parse_mode="Markdown"
        )
        return

    if text_lower in ["📤 catat keluar"]:
        await update.message.reply_text(
            "🔴 Kirim pengeluaran dengan format:\n`keluar [jumlah] [kategori]`\n\nContoh: `keluar 25000 Makan`",
            parse_mode="Markdown"
        )
        return

    # Parse transaksi
    result = parse_transaksi(text)
    if result:
        tipe, kategori, jumlah, catatan = result
        try:
            add_transaction(tipe, kategori, jumlah, catatan)
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
        except Exception as e:
            logger.error(f"Error saving: {e}")
            await update.message.reply_text("❌ Gagal menyimpan. Coba lagi ya.")
    else:
        await update.message.reply_text(
            "🤔 Format tidak dikenali.\n\n"
            "Contoh yang benar:\n"
            "`masuk 50000 Gaji`\n"
            "`keluar 25000 Makan`\n\n"
            "Ketik *Bantuan* untuk panduan lengkap.",
            parse_mode="Markdown"
        )

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bantuan", bantuan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
