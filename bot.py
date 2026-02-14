import os
import json
import asyncio
import logging
import sys
import time
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
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', '')

# Multiple RPC endpoints for load balancing
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.publicnode.com",
]

# Known token symbols (you can expand this list)
TOKEN_SYMBOLS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "So11111111111111111111111111111111111111112": "wSOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "RAYJ4K9FnTkn4D6QbKj6P5LvJZf9JqWzXqXqXqXqXqX": "RAY",
    "SRMuApVNdxXokk5GT7XD5cUUgXMBCoAz2LHeuAoKWRt": "SRM",
}

# ==================== SETUP LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== FLASK APP FOR WEBHOOK ====================
flask_app = Flask(__name__)

# ==================== TELEGRAM BOT SETUP ====================
telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()
user_states = {}

# ==================== HELPER FUNCTIONS ====================
def format_private_key(private_key_bytes):
    """Format private key in multiple formats"""
    base58_key = base58.b58encode(private_key_bytes).decode()
    json_array = list(private_key_bytes)
    return {'base58': base58_key, 'json_array': json_array}

def get_token_symbol(mint_address):
    """Get token symbol from mint address"""
    return TOKEN_SYMBOLS.get(mint_address, mint_address[:8] + "...")

async def check_sol_balance(session, address, endpoint):
    """Check SOL balance for an address"""
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
    except Exception as e:
        logger.debug(f"SOL balance check error for {address}: {e}")
    return 0

async def check_spl_tokens(session, address, endpoint):
    """Check all SPL tokens for an address"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }
        
        async with session.post(endpoint, json=payload, timeout=15) as response:
            if response.status == 200:
                data = await response.json()
                tokens = []
                
                if 'result' in data and 'value' in data['result']:
                    for account in data['result']['value']:
                        try:
                            # Parse token account info
                            parsed = account['account']['data']['parsed']
                            if parsed['program'] == 'spl-token' and parsed['type'] == 'account':
                                info = parsed['info']
                                token_amount = info['tokenAmount']
                                balance = float(token_amount['uiAmount'])
                                
                                if balance > 0:
                                    mint = info['mint']
                                    tokens.append({
                                        'mint': mint,
                                        'balance': balance,
                                        'symbol': get_token_symbol(mint),
                                        'decimals': token_amount['decimals'],
                                        'token_account': account['pubkey']
                                    })
                        except Exception as e:
                            logger.debug(f"Error parsing token account: {e}")
                            continue
                
                return tokens
    except Exception as e:
        logger.debug(f"SPL token check error for {address}: {e}")
    return []

async def scan_wallets_parallel(seed_phrase, update):
    """Scan wallets in parallel for both SOL and SPL tokens"""
    try:
        seed = Bip39SeedGenerator(seed_phrase).Generate()
        
        await update.message.reply_text("🔑 Deriving wallet addresses...")
        
        # Derive all wallets first
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
        
        await update.message.reply_text(f"🔍 Scanning {MAX_WALLETS} wallets for SOL and SPL tokens...")
        
        found_count = 0
        batch_size = 10  # Smaller batch size because SPL checks are heavier
        
        async with aiohttp.ClientSession() as session:
            for batch_start in range(0, MAX_WALLETS, batch_size):
                batch_end = min(batch_start + batch_size, MAX_WALLETS)
                batch = wallets[batch_start:batch_end]
                
                # Create tasks for both SOL and SPL checks
                tasks = []
                for wallet in batch:
                    endpoint = RPC_ENDPOINTS[wallet['index'] % len(RPC_ENDPOINTS)]
                    tasks.append(asyncio.gather(
                        check_sol_balance(session, wallet['address'], endpoint),
                        check_spl_tokens(session, wallet['address'], endpoint)
                    ))
                
                # Wait for all checks in batch to complete
                results = await asyncio.gather(*tasks)
                
                # Process results
                for wallet, (sol_balance, spl_tokens) in zip(batch, results):
                    has_funds = sol_balance > 0 or len(spl_tokens) > 0
                    
                    if has_funds:
                        found_count += 1
                        logger.info(f"Found wallet {wallet['index']} with SOL: {sol_balance}, Tokens: {len(spl_tokens)}")
                        
                        # Build comprehensive message
                        message = f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                        message += f"📌 *Account Index:* `{wallet['index']}`\n"
                        message += f"📬 *Address:* `{wallet['address']}`\n"
                        message += f"💰 *SOL Balance:* `{sol_balance:.6f} SOL`\n"
                        
                        if spl_tokens:
                            message += f"\n🪙 *SPL Tokens:*\n"
                            for token in spl_tokens:
                                message += f"• *{token['symbol']}*: `{token['balance']}` (Mint: `{token['mint'][:8]}...`)\n"
                        
                        message += f"\n*📥 HOW TO IMPORT INTO PHANTOM:*\n"
                        message += f"1. Copy the Base58 key below\n"
                        message += f"2. Open Phantom → Add Account → Import Private Key\n"
                        message += f"3. Paste the key and click Import\n\n"
                        message += f"🔐 *BASE58 PRIVATE KEY:*\n"
                        message += f"`{wallet['private_key']['base58']}`\n\n"
                        message += f"*(JSON format if needed)*\n"
                        message += f"`{wallet['private_key']['json_array']}`"
                        
                        # Send message (split if too long)
                        if len(message) > 4000:
                            # Send summary first
                            await update.message.reply_text(
                                f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                                f"📌 Index: `{wallet['index']}`\n"
                                f"💰 SOL: `{sol_balance:.6f}`\n"
                                f"🪙 Tokens: {len(spl_tokens)}",
                                parse_mode='Markdown'
                            )
                            # Send private key separately
                            await update.message.reply_text(
                                f"🔐 *BASE58 PRIVATE KEY:*\n"
                                f"`{wallet['private_key']['base58']}`",
                                parse_mode='Markdown'
                            )
                        else:
                            await update.message.reply_text(message, parse_mode='Markdown')
                
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
        "*Features:*\n"
        f"• Scans {MAX_WALLETS} wallets\n"
        "• Checks **SOL balance**\n"
        "• Checks **ALL SPL tokens** (USDC, USDT, BONK, etc.)\n"
        "• Parallel scanning for speed\n\n"
        "*Commands:*\n"
        "/scan - Start scanning\n"
        "/cancel - Cancel current operation\n\n"
        "*📥 HOW TO IMPORT FOUND WALLETS:*\n"
        "1. Copy the Base58 private key\n"
        "2. Open Phantom → Add Account → Import Private Key\n"
        "3. Paste the key and click Import\n\n"
        "⚠️ *Security:* Your seed phrase is never stored.",
        parse_mode='Markdown'
    )

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command"""
    chat_id = update.effective_chat.id
    user_states[chat_id] = {'awaiting_seed': True}
    logger.info(f"Scan command from chat {chat_id}")
    await update.message.reply_text(
        f"📝 *Please send your 12 or 24-word seed phrase:*\n\n"
        f"I'll scan the first {MAX_WALLETS} wallets for:\n"
        f"• SOL balance\n"
        f"• All SPL tokens (USDC, USDT, BONK, etc.)\n\n"
        f"This may take 20-30 seconds.",
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
        await update.message.reply_text("✅ Seed phrase received. Starting comprehensive scan...")
        
        try:
            # Start timing
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
                f"*Remember:* Check each found wallet for SOL and SPL tokens!",
                parse_mode='Markdown'
            )
            logger.info(f"Scan complete for chat {chat_id}, found {found_count} wallets in {elapsed_time:.1f}s")
            
        except Exception as e:
            error_msg = f"Error during scan: {str(e)}"
            logger.error(error_msg)
            await update.message.reply_text(f"❌ Error: {str(e)}")
        
        # Clear user state
        del user_states[chat_id]

# ==================== ADD HANDLERS ====================
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("scan", scan))
telegram_app.add_handler(CommandHandler("cancel", cancel))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ==================== WEBHOOK PROCESSING ====================
async def process_update(update_data):
    """Process Telegram update asynchronously"""
    try:
        # Initialize the application if not already initialized
        if not telegram_app._initialized:
            logger.info("Initializing telegram application...")
            await telegram_app.initialize()
            logger.info("Application initialized successfully")
        
        update = Update.de_json(update_data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")

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

# ==================== SETUP FUNCTION ====================
async def init_and_setup():
    """Initialize application and set webhook"""
    try:
        # Initialize the application
        await telegram_app.initialize()
        logger.info("✅ Application initialized")
        
        # Set webhook if URL is configured
        if RENDER_URL:
            webhook_url = f"{RENDER_URL}/webhook"
            await telegram_app.bot.delete_webhook()
            success = await telegram_app.bot.set_webhook(url=webhook_url)
            if success:
                logger.info(f"✅ Webhook set to {webhook_url}")
            else:
                logger.error("❌ Failed to set webhook")
    except Exception as e:
        logger.error(f"❌ Setup error: {e}")

# ==================== MAIN ====================
if __name__ == "__main__":
    logger.info("🚀 Starting Solana Wallet Finder Bot with SPL token support...")
    logger.info(f"📊 Will scan {MAX_WALLETS} wallets per request")
    logger.info(f"🌐 Using {len(RPC_ENDPOINTS)} RPC endpoints for load balancing")
    logger.info(f"🪙 Tracking {len(TOKEN_SYMBOLS)} known SPL tokens")
    logger.info(f"🤖 Bot token exists: {bool(BOT_TOKEN)}")
    
    # Initialize the application and set webhook
    asyncio.run(init_and_setup())
    
    # Run Flask app
    flask_app.run(host='0.0.0.0', port=PORT)
