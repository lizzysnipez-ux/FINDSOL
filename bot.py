import os
import json
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solana.rpc.api import Client
from solders.pubkey import Pubkey
import base58

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RPC_ENDPOINT = "https://api.mainnet-beta.solana.com"
MAX_WALLETS = 100

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize application
application = Application.builder().token(BOT_TOKEN).build()
user_states = {}

def format_private_key(private_key_bytes):
    """Format private key in multiple formats"""
    base58_key = base58.b58encode(private_key_bytes).decode()
    json_array = list(private_key_bytes)
    return {'base58': base58_key, 'json_array': json_array}

def check_balance(address):
    """Check SOL balance for an address"""
    try:
        client = Client(RPC_ENDPOINT)
        pubkey = Pubkey.from_string(address)
        balance_response = client.get_balance(pubkey)
        
        if hasattr(balance_response, 'value'):
            return balance_response.value / 1e9
        elif 'result' in balance_response:
            return balance_response['result']['value'] / 1e9
        return 0
    except Exception as e:
        logger.error(f"Balance check error: {e}")
        return 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    await update.message.reply_text(
        "👋 *Welcome to Solana Wallet Finder!*\n\n"
        "This bot helps you recover wallets from your seed phrase.\n\n"
        "*Commands:*\n"
        "/scan - Start scanning for wallets\n"
        "/cancel - Cancel current operation\n\n"
        "⚠️ *Security:* Your seed phrase is only used temporarily and never stored.",
        parse_mode='Markdown'
    )

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command"""
    chat_id = update.effective_chat.id
    user_states[chat_id] = {'awaiting_seed': True}
    await update.message.reply_text(
        "📝 *Please send your 12 or 24-word seed phrase:*\n\n"
        "I'll scan the first 100 wallets for any funds.",
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command"""
    chat_id = update.effective_chat.id
    if chat_id in user_states:
        del user_states[chat_id]
        await update.message.reply_text("✅ Operation cancelled.")
    else:
        await update.message.reply_text("No active operation to cancel.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages (seed phrases)"""
    chat_id = update.effective_chat.id
    message = update.message.text
    
    if chat_id in user_states and user_states[chat_id].get('awaiting_seed'):
        await update.message.reply_text("✅ Seed phrase received. Starting scan...")
        
        found_count = 0
        try:
            seed = Bip39SeedGenerator(message).Generate()
            
            for i in range(MAX_WALLETS):
                # Send progress updates every 10 wallets
                if i > 0 and i % 10 == 0:
                    await update.message.reply_text(
                        f"📊 Progress: {i}/{MAX_WALLETS} wallets... (Found: {found_count})"
                    )
                
                # Derive wallet
                bip44_mst = Bip44.FromSeed(seed, Bip44Coins.SOLANA)
                bip44_acc = bip44_mst.Purpose().Coin().Account(i)
                bip44_change = bip44_acc.Change(Bip44Changes.CHAIN_EXT)
                
                address = bip44_change.PublicKey().ToAddress()
                private_key_bytes = bip44_change.PrivateKey().Raw().ToBytes()
                private_key = format_private_key(private_key_bytes)
                
                # Check balance
                balance = check_balance(address)
                
                if balance > 0:
                    found_count += 1
                    await update.message.reply_text(
                        f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                        f"📌 *Account Index:* `{i}`\n"
                        f"📬 *Address:* `{address}`\n"
                        f"💰 *Balance:* `{balance} SOL`\n\n"
                        f"🔐 *Private Key (JSON for Phantom):*\n"
                        f"`{private_key['json_array']}`\n\n"
                        f"🔐 *Private Key (Base58):*\n"
                        f"`{private_key['base58']}`\n\n"
                        f"⚠️ *SAVE THIS AND DELETE THIS MESSAGE*",
                        parse_mode='Markdown'
                    )
            
            await update.message.reply_text(
                f"✅ *Scan Complete!*\n\n"
                f"• Wallets scanned: {MAX_WALLETS}\n"
                f"• Wallets with funds: {found_count}",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            logger.error(f"Scan error: {e}")
        
        # Clear user state
        del user_states[chat_id]

# Add handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan))
application.add_handler(CommandHandler("cancel", cancel))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Webhook handler for GitHub Actions
async def webhook_handler(event, context):
    """Handle incoming webhook requests"""
    try:
        if event.get('httpMethod') == 'POST':
            body = json.loads(event.get('body', '{}'))
            update = Update.de_json(body, application.bot)
            await application.process_update(update)
            return {'statusCode': 200, 'body': json.dumps({'ok': True})}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return {'statusCode': 200, 'body': json.dumps({'ok': True})}

# Entry point for GitHub Actions
def run_webhook(event, context):
    """Called by GitHub Actions"""
    return asyncio.run(webhook_handler(event, context))

# For local testing
if __name__ == "__main__":
    print("Starting bot in polling mode for local testing...")
    application.run_polling()
