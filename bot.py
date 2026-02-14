import os
import json
import asyncio
import logging
import sys
import time
import random
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solana.rpc.api import Client
from solders.pubkey import Pubkey
import base58
from datetime import datetime
from flask import Flask
import threading

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_WALLETS = 100

# Multiple RPC endpoints for load balancing
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.publicnode.com",
    "https://api.mainnet.rpcpool.com",
]

# ==================== SETUP LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== FLASK HEALTH CHECK ====================
health_app = Flask(__name__)

@health_app.route('/')
@health_app.route('/health')
def health():
    return 'Bot is running!'

def run_health_server():
    health_app.run(host='0.0.0.0', port=10000)

# Start health server in background thread
threading.Thread(target=run_health_server, daemon=True).start()

# ==================== TELEGRAM BOT SETUP ====================
application = Application.builder().token(BOT_TOKEN).build()
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
                # Rate limited - wait and retry with different endpoint
                await asyncio.sleep(1)
                new_endpoint = RPC_ENDPOINTS[(RPC_ENDPOINTS.index(endpoint) + 1) % len(RPC_ENDPOINTS)]
                return await check_balance_async(session, address, new_endpoint, retry_count + 1)
    except asyncio.TimeoutError:
        if retry_count < 2:
            await asyncio.sleep(0.5)
            new_endpoint = RPC_ENDPOINTS[(RPC_ENDPOINTS.index(endpoint) + 1) % len(RPC_ENDPOINTS)]
            return await check_balance_async(session, address, new_endpoint, retry_count + 1)
    except Exception as e:
        logger.debug(f"Balance check error for {address}: {e}")
    
    return 0

async def scan_wallets_parallel(seed_phrase, update):
    """Scan wallets in parallel for maximum speed"""
    try:
        seed = Bip39SeedGenerator(seed_phrase).Generate()
        
        # Step 1: Derive all addresses first (this is very fast)
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
        
        await update.message.reply_text(f"🔍 Scanning {MAX_WALLETS} wallets with 10 parallel connections...")
        
        # Step 2: Scan in parallel batches
        found_count = 0
        batch_size = 20  # Scan 20 wallets at a time
        
        async with aiohttp.ClientSession() as session:
            for batch_start in range(0, MAX_WALLETS, batch_size):
                batch_end = min(batch_start + batch_size, MAX_WALLETS)
                batch = wallets[batch_start:batch_end]
                
                # Create tasks for this batch
                tasks = []
                for wallet in batch:
                    # Distribute requests across different endpoints
                    endpoint = RPC_ENDPOINTS[wallet['index'] % len(RPC_ENDPOINTS)]
                    tasks.append(check_balance_async(session, wallet['address'], endpoint))
                
                # Run all balance checks in parallel
                balances = await asyncio.gather(*tasks)
                
                # Process results
                batch_found = 0
                for wallet, balance in zip(batch, balances):
                    if balance and balance > 0:
                        found_count += 1
                        batch_found += 1
                        logger.info(f"Found wallet {wallet['index']} with {balance} SOL")
                        
                        # Send wallet details
                        await update.message.reply_text(
                            f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                            f"📌 *Account Index:* `{wallet['index']}`\n"
                            f"📬 *Address:* `{wallet['address']}`\n"
                            f"💰 *Balance:* `{balance} SOL`\n\n"
                            f"🔐 *Private Key (JSON):*\n"
                            f"`{wallet['private_key']['json_array']}`\n\n"
                            f"🔐 *Private Key (Base58):*\n"
                            f"`{wallet['private_key']['base58']}`\n\n"
                            f"⚠️ *SAVE THIS AND DELETE THIS MESSAGE*",
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
        "⚡ *Features:*\n"
        f"• Scans {MAX_WALLETS} wallets in ~15 seconds\n"
        "• Parallel scanning for maximum speed\n"
        "• Multiple RPC endpoints to avoid rate limits\n\n"
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
        f"I'll scan the first {MAX_WALLETS} wallets at high speed.",
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
                f"Use /scan to try another seed phrase.",
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
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan))
application.add_handler(CommandHandler("cancel", cancel))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ==================== MAIN ====================
if __name__ == "__main__":
    print("🚀 Starting Solana Wallet Finder Bot...")
    print(f"📊 Will scan {MAX_WALLETS} wallets per request")
    print(f"🌐 Using {len(RPC_ENDPOINTS)} RPC endpoints for load balancing")
    print(f"🤖 Bot token exists: {bool(BOT_TOKEN)}")
    
    # Start the bot
    application.run_polling()
