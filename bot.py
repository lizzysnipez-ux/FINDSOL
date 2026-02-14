import os
import json
import asyncio
import logging
import sys
import aiohttp
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solders.pubkey import Pubkey
import base58
from datetime import datetime

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_WALLETS = 100
PORT = int(os.environ.get('PORT', 10000))
# Your Render URL - set this in Render environment variables
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', '')

# Multiple RPC endpoints for load balancing
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.publicnode.com",
]

# ==================== SETUP LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== FLASK APP FOR WEBHOOK ====================
flask_app = Flask(__name__)

# ==================== TELEGRAM BOT SETUP ====================
# Important: No updater for webhook mode!
telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()
user_states = {}

# ==================== HELPER FUNCTIONS ====================
def format_private_key(private_key_bytes):
    """Format private key in multiple formats"""
    base58_key = base58.b58encode(private_key_bytes).decode()
    json_array = list(private_key_bytes)
    return {'base58': base58_key, 'json_array': json_array}

async def check_balance_async(session, address, endpoint, retry_count=0):
    """Async balance check with retry logic"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address]
        }
        
        async with session.post(endpoint, json=payload, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                if 'result' in data:
                    return data['result']['value'] / 1e9
            elif response.status == 429 and retry_count < 2:
                await asyncio.sleep(1)
                new_endpoint = RPC_ENDPOINTS[(RPC_ENDPOINTS.index(endpoint) + 1) % len(RPC_ENDPOINTS)]
                return await check_balance_async(session, address, new_endpoint, retry_count + 1)
    except Exception as e:
        logger.debug(f"Balance check error for {address}: {e}")
    
    return 0

async def scan_wallets_parallel(seed_phrase, update):
    """Scan wallets in parallel for maximum speed"""
    try:
        seed = Bip39SeedGenerator(seed_phrase).Generate()
        
        await update.message.reply_text("🔑 Deriving wallet addresses...")
        
        wallets = []
        for i in range(MAX_WALLETS):
            bip44_mst = Bip44.FromSeed(seed, Bip44Coins.SOLANA)
            bip44_acc = bip44_mst.Purpose().Coin().Account(i)
            bip44_change = bip44_acc.Change(Bip44Changes.CHAIN_EXT)
            
            address = bip44_change.PublicKey().ToAddress()
            private_key_bytes = bip44_change.PrivateKey().Raw().ToBytes()
            private_key = format_private_key(private_key_bytes)
            
            wallets.append({
                'index': i,
                'address': address,
                'private_key': private_key
            })
        
        await update.message.reply_text(f"🔍 Scanning {MAX_WALLETS} wallets with parallel connections...")
        
        found_count = 0
        batch_size = 20
        
        async with aiohttp.ClientSession() as session:
            for batch_start in range(0, MAX_WALLETS, batch_size):
                batch_end = min(batch_start + batch_size, MAX_WALLETS)
                batch = wallets[batch_start:batch_end]
                
                tasks = []
                for wallet in batch:
                    endpoint = RPC_ENDPOINTS[wallet['index'] % len(RPC_ENDPOINTS)]
                    tasks.append(check_balance_async(session, wallet['address'], endpoint))
                
                balances = await asyncio.gather(*tasks)
                
                batch_found = 0
                for wallet, balance in zip(batch, balances):
                    if balance and balance > 0:
                        found_count += 1
                        batch_found += 1
                        logger.info(f"Found wallet {wallet['index']} with {balance} SOL")
                        
                        # ===== FIXED PRIVATE KEY FORMAT =====
                        # Get the complete Base58 key (no truncation)
                        base58_key = wallet['private_key']['base58']
                        
                        # Send wallet found message with clear import instructions
                        await update.message.reply_text(
                            f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                            f"📌 *Account Index:* `{wallet['index']}`\n"
                            f"📬 *Address:* `{wallet['address']}`\n"
                            f"💰 *Balance:* `{balance:.6f} SOL`\n\n"
                            f"*📥 HOW TO IMPORT INTO PHANTOM:*\n"
                            f"1. Copy the Base58 key below (the whole long string)\n"
                            f"2. Open Phantom → Click profile icon → Add Account\n"
                            f"3. Select *Import Private Key* (NOT Recovery Phrase)\n"
                            f"4. Paste the key and click Import\n\n"
                            f"🔐 *BASE58 PRIVATE KEY (USE THIS FOR PHANTOM):*\n"
                            f"`{base58_key}`\n\n"
                            f"*(Alternative format - JSON array, if needed)*\n"
                            f"`{wallet['private_key']['json_array']}`",
                            parse_mode='Markdown'
                        )
                
                # Send progress update
                await update.message.reply_text(
                    f"📊 Progress: {batch_end}/{MAX_WALLETS} wallets... (Found: {found_count})"
                )
        
        return found_count
        
    except Exception as e:
        logger.error(f"Scan error: {e}")
        raise

# ==================== TELEGRAM COMMAND HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    logger.info(f"Start command from user {user.id}")
    await update.message.reply_text(
        "👋 *Welcome to Solana Wallet Finder!*\n\n"
        "This bot helps you recover wallets from your seed phrase.\n\n"
        "*Commands:*\n"
        "/scan - Start scanning for wallets\n"
        "/cancel - Cancel current operation\n\n"
        "*📥 HOW TO IMPORT FOUND WALLETS:*\n"
        "When a wallet is found, copy the Base58 private key and:\n"
        "1. Open Phantom wallet\n"
        "2. Click profile icon → Add Account\n"
        "3. Select *Import Private Key* (NOT Recovery Phrase)\n"
        "4. Paste the key and click Import\n\n"
        "⚠️ *Security:* Your seed phrase is only used temporarily.",
        parse_mode='Markdown'
    )

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command"""
    chat_id = update.effective_chat.id
    user_states[chat_id] = {'awaiting_seed': True}
    logger.info(f"Scan command from chat {chat_id}")
    await update.message.reply_text(
        f"📝 *Please send your 12 or 24-word seed phrase:*\n\n"
        f"I'll scan the first {MAX_WALLETS} wallets at high speed.\n\n"
        f"⚠️ *Important:* Make sure to copy the full Base58 key when wallets are found!",
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command"""
    chat_id = update.effective_chat.id
    if chat_id in user_states:
        del user_states[chat_id]
        await update.message.reply_text("✅ Operation cancelled.")
        logger.info(f"Cancelled operation for chat {chat_id}")
    else:
        await update.message.reply_text("No active operation to cancel.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages (seed phrases)"""
    chat_id = update.effective_chat.id
    message = update.message.text
    
    if chat_id in user_states and user_states[chat_id].get('awaiting_seed'):
        logger.info(f"Received seed phrase from chat {chat_id}")
        await update.message.reply_text("✅ Seed phrase received. Starting high-speed scan...")
        
        try:
            # Start timing
            import time
            start_time = time.time()
            
            # Run the parallel scan
            found_count = await scan_wallets_parallel(message, update)
            
            # Calculate time taken
            elapsed_time = time.time() - start_time
            
            await update.message.reply_text(
                f"✅ *Scan Complete!*\n\n"
                f"• Wallets scanned: {MAX_WALLETS}\n"
                f"• Wallets with funds: {found_count}\n"
                f"• Time taken: {elapsed_time:.1f} seconds\n\n"
                f"*Remember:* Use the Base58 private key to import into Phantom!",
                parse_mode='Markdown'
            )
            logger.info(f"Scan complete for chat {chat_id}, found {found_count} wallets in {elapsed_time:.1f}s")
            
        except Exception as e:
            error_msg = f"Error during scan: {str(e)}"
            logger.error(error_msg)
            await update.message.reply_text(f"❌ Error: {str(e)}")
        
        # Clear user state
        del user_states[chat_id]

# ==================== WEBHOOK HANDLERS ====================
@flask_app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates"""
    try:
        update_data = request.get_json()
        logger.info(f"Received webhook update: {update_data.get('update_id')}")
        
        # Process the update asynchronously
        asyncio.run(process_update(update_data))
        
        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'OK', 200

@flask_app.route('/health')
@flask_app.route('/')
def health():
    """Health check endpoint for Render"""
    return 'Bot is running!', 200

async def process_update(update_data):
    """Process Telegram update asynchronously"""
    try:
        update = Update.de_json(update_data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")

# ==================== ADD HANDLERS ====================
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("scan", scan))
telegram_app.add_handler(CommandHandler("cancel", cancel))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ==================== SETUP WEBHOOK ====================
async def setup_webhook():
    """Set the webhook on startup"""
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        try:
            await telegram_app.bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")

# ==================== MAIN ====================
if __name__ == "__main__":
    logger.info("🚀 Starting Solana Wallet Finder Bot with webhook...")
    logger.info(f"📊 Will scan {MAX_WALLETS} wallets per request")
    logger.info(f"🌐 Using {len(RPC_ENDPOINTS)} RPC endpoints for load balancing")
    logger.info(f"🤖 Bot token exists: {bool(BOT_TOKEN)}")
    
    # Set webhook asynchronously
    asyncio.run(setup_webhook())
    
    # Run Flask app (this blocks)
    flask_app.run(host='0.0.0.0', port=PORT)
