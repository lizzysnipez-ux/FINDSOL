import sys
import traceback
import os
import json
import asyncio
import logging
import time
import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, Bip39MnemonicValidator, Bip39MnemonicError
from solders.pubkey import Pubkey
import base58
from datetime import datetime

# Try to import nacl for key conversion
try:
    import nacl.signing
    import nacl.encoding
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    print("WARNING: pynacl not installed, private keys will not be compatible with Phantom!", file=sys.stderr)

# ==================== STARTUP ERROR HANDLING ====================
try:
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

    # Known token symbols
    TOKEN_SYMBOLS = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
        "So11111111111111111111111111111111111111112": "wSOL",
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    }

    # ==================== SETUP LOGGING ====================
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)

    # ==================== TELEGRAM BOT SETUP ====================
    logger.info("1️⃣ Creating Telegram application...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    user_states = {}

    # ==================== HELPER FUNCTIONS ====================
    def validate_seed_phrase(seed_phrase):
        """
        Validate a BIP39 seed phrase
        Returns (is_valid, error_message)
        """
        try:
            # Clean the phrase
            cleaned = ' '.join(seed_phrase.strip().split())
            words = cleaned.split()
            
            # Check word count
            if len(words) not in [12, 15, 18, 21, 24]:
                return False, f"Invalid word count: {len(words)}. Expected 12, 15, 18, 21, or 24 words."
            
            # Validate the mnemonic
            validator = Bip39MnemonicValidator()
            if validator.IsValid(cleaned):
                return True, "Valid seed phrase"
            else:
                # Try to get more specific error
                try:
                    # This will throw a more specific error
                    Bip39SeedGenerator(cleaned).Generate()
                    return True, "Valid seed phrase"
                except Bip39MnemonicError as e:
                    return False, str(e)
                except Exception as e:
                    return False, f"Invalid checksum"
                    
        except Bip39MnemonicError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Invalid seed phrase: {str(e)}"

    def format_private_key(private_key_bytes):
        """
        Format private key as Ed25519 keypair for Phantom (64 bytes)
        
        bip_utils gives us 32 bytes (seed), but Phantom needs the full 64-byte keypair:
        - First 32 bytes: private key seed
        - Last 32 bytes: public key
        """
        try:
            if HAS_NACL and len(private_key_bytes) == 32:
                # Convert 32-byte seed to Ed25519 signing key
                signing_key = nacl.signing.SigningKey(private_key_bytes)
                
                # Get the full keypair (64 bytes)
                # signing_key.encode() = 32 bytes private key seed
                # signing_key.verify_key.encode() = 32 bytes public key
                keypair_bytes = signing_key.encode() + signing_key.verify_key.encode()
                
                # Encode to Base58
                base58_key = base58.b58encode(keypair_bytes).decode()
                json_array = list(keypair_bytes)
                
                logger.info(f"✅ Generated valid keypair: {len(base58_key)} chars (should be 87-88)")
                
                return {
                    'base58': base58_key,
                    'json_array': json_array,
                    'seed': base58.b58encode(private_key_bytes).decode()  # Keep seed for debugging
                }
            else:
                # Fallback to raw seed format (44 chars) - not Phantom compatible!
                logger.warning(f"Using raw seed format ({len(private_key_bytes)} bytes) - NOT Phantom compatible!")
                base58_key = base58.b58encode(private_key_bytes).decode()
                json_array = list(private_key_bytes)
                return {'base58': base58_key, 'json_array': json_array}
        except Exception as e:
            logger.error(f"Key conversion error: {e}")
            base58_key = base58.b58encode(private_key_bytes).decode()
            json_array = list(private_key_bytes)
            return {'base58': base58_key, 'json_array': json_array}

    def get_token_symbol(mint_address):
        """Get token symbol from mint address"""
        return TOKEN_SYMBOLS.get(mint_address, mint_address[:8] + "...")

    async def check_sol_balance(session, address, endpoints, retries=2):
        """Check SOL balance for an address with retry logic and multiple endpoints"""
        for attempt in range(retries):
            # Try different endpoints in round-robin fashion
            for endpoint in endpoints:
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
                            elif 'error' in data:
                                logger.debug(f"RPC error from {endpoint}: {data['error']}")
                        elif response.status == 429:  # Rate limited
                            logger.debug(f"Rate limited by {endpoint}, trying next...")
                            await asyncio.sleep(1)
                            continue
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout from {endpoint}")
                    continue
                except Exception as e:
                    logger.debug(f"Error from {endpoint}: {e}")
                    continue
            
            # If we've tried all endpoints and still failed, wait before retry
            if attempt < retries - 1:
                await asyncio.sleep(2)
        
        return 0

    async def check_spl_tokens(session, address, endpoints, retries=2):
        """Check SPL tokens with retry logic and multiple endpoints"""
        for attempt in range(retries):
            for endpoint in endpoints:
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
                                                })
                                    except Exception:
                                        continue
                            
                            return tokens
                        elif response.status == 429:
                            logger.debug(f"Rate limited by {endpoint}, trying next...")
                            await asyncio.sleep(1)
                            continue
                except asyncio.TimeoutError:
                    logger.debug(f"Timeout from {endpoint}")
                    continue
                except Exception:
                    continue
            
            if attempt < retries - 1:
                await asyncio.sleep(2)
        
        return []

    async def scan_wallets_parallel(seed_phrase, update, context):
        """Scan wallets in parallel with duplicate prevention"""
        try:
            seed = Bip39SeedGenerator(seed_phrase).Generate()
            
            status_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="🔑 Deriving wallet addresses..."
            )
            
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
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"🔍 Racing {len(RPC_ENDPOINTS)} RPC endpoints..."
            )
            
            found_count = 0
            reported_wallets = set()
            batch_size = 20
            start_time = time.time()
            
            async with aiohttp.ClientSession() as session:
                for batch_start in range(0, MAX_WALLETS, batch_size):
                    batch_end = min(batch_start + batch_size, MAX_WALLETS)
                    batch = wallets[batch_start:batch_end]
                    
                    # Create tasks for this batch
                    tasks = []
                    for wallet in batch:
                        tasks.append(asyncio.gather(
                            check_sol_balance(session, wallet['address'], RPC_ENDPOINTS),
                            check_spl_tokens(session, wallet['address'], RPC_ENDPOINTS)
                        ))
                    
                    results = await asyncio.gather(*tasks)
                    
                    # Process results
                    for wallet, (sol_balance, spl_tokens) in zip(batch, results):
                        if wallet['index'] in reported_wallets:
                            continue
                        
                        has_funds = sol_balance > 0 or len(spl_tokens) > 0
                        
                        if has_funds:
                            found_count += 1
                            reported_wallets.add(wallet['index'])
                            
                            key_length = len(wallet['private_key']['base58'])
                            key_valid = "✅" if key_length > 80 else "❌"
                            
                            message = f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                            message += f"📌 *Account Index:* `{wallet['index']}`\n"
                            message += f"📬 *Address:* `{wallet['address']}`\n"
                            message += f"💰 *SOL Balance:* `{sol_balance:.6f} SOL`\n"
                            
                            if spl_tokens:
                                message += f"\n🪙 *SPL Tokens:*\n"
                                for token in spl_tokens:
                                    message += f"• *{token['symbol']}*: `{token['balance']}`\n"
                            
                            message += f"\n🔐 *BASE58 PRIVATE KEY (length: {key_length} chars) {key_valid}:*\n"
                            message += f"`{wallet['private_key']['base58']}`\n\n"
                            
                            if key_length > 80:
                                message += f"*✅ This key is valid for Phantom! Import instructions:*\n"
                            else:
                                message += f"*❌ This key is too short! pynacl may not be installed.*\n"
                            
                            message += f"1. Copy the ENTIRE key above\n"
                            message += f"2. Open Phantom → Add Account → Import Private Key\n"
                            message += f"3. Paste and click Import"
                            
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=message,
                                parse_mode='Markdown'
                            )
                    
                    # Update progress
                    elapsed = time.time() - start_time
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=status_msg.message_id,
                        text=f"📊 Progress: {batch_end}/{MAX_WALLETS} wallets | Found: {found_count} | Time: {elapsed:.0f}s"
                    )
            
            total_time = time.time() - start_time
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"✅ *Scan Complete!*\n\n"
                     f"• Scanned: {MAX_WALLETS} wallets\n"
                     f"• Found: {found_count} wallets with funds\n"
                     f"• Time: {total_time:.1f} seconds",
                parse_mode='Markdown'
            )
            
            return found_count
            
        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Error during scan: {str(e)}"
            )
            return 0

    # ==================== TELEGRAM COMMAND HANDLERS ====================
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        logger.info(f"Start command from user {user.id}")
        
        nacl_status = "✅ Installed" if HAS_NACL else "❌ NOT INSTALLED - Keys will NOT work in Phantom!"
        
        await update.message.reply_text(
            "👋 *Welcome to Solana Wallet Finder!*\n\n"
            "• Scans 100 wallets\n"
            "• Checks SOL + ALL SPL tokens\n"
            "• Parallel scanning for speed\n"
            "• Validates seed phrases before scanning\n\n"
            f"*🔐 Key Format Status:* {nacl_status}\n\n"
            "/scan - Start scanning\n"
            "/cancel - Cancel\n\n"
            "*📥 IMPORT INSTRUCTIONS:*\n"
            "1. Copy the ENTIRE Base58 private key\n"
            "2. Open Phantom → Add Account → Import Private Key\n"
            "3. Paste the key and click Import",
            parse_mode='Markdown'
        )

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /scan command"""
        chat_id = update.effective_chat.id
        user_states[chat_id] = {'awaiting_seed': True}
        logger.info(f"Scan command from chat {chat_id}")
        
        nacl_warning = "" if HAS_NACL else "\n\n⚠️ *Warning:* pynacl not installed - private keys will NOT work in Phantom!"
        
        await update.message.reply_text(
            f"📝 *Send your 12-word seed phrase:*\n\n"
            f"I'll scan {MAX_WALLETS} wallets for SOL + all SPL tokens.{nacl_warning}",
            parse_mode='Markdown'
        )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command"""
        chat_id = update.effective_chat.id
        if chat_id in user_states:
            del user_states[chat_id]
            await update.message.reply_text("✅ Cancelled.")
            logger.info(f"Cancelled for chat {chat_id}")
        else:
            await update.message.reply_text("No active operation.")

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages (seed phrases)"""
        chat_id = update.effective_chat.id
        message = update.message.text
        
        if chat_id in user_states and user_states[chat_id].get('awaiting_seed'):
            logger.info(f"Received seed phrase from chat {chat_id}")
            
            # Validate the seed phrase first
            is_valid, error_msg = validate_seed_phrase(message)
            
            if not is_valid:
                await update.message.reply_text(
                    f"❌ *Invalid seed phrase:* {error_msg}\n\n"
                    f"Please check the phrase and try again.\n"
                    f"Common issues:\n"
                    f"• Misspelled words\n"
                    f"• Wrong word order\n"
                    f"• Missing words\n"
                    f"• Extra spaces\n\n"
                    f"Use /cancel to stop",
                    parse_mode='Markdown'
                )
                # Don't clear state - let them try again
                return
            
            await update.message.reply_text("✅ Starting scan...")
            
            try:
                start_time = time.time()
                found_count = await scan_wallets_parallel(message, update, context)
                elapsed_time = time.time() - start_time
                
                logger.info(f"Scan complete for chat {chat_id}, found {found_count} wallets in {elapsed_time:.1f}s")
                
            except Bip39MnemonicError as e:
                logger.error(f"Seed phrase error: {e}")
                await update.message.reply_text(
                    f"❌ Invalid seed phrase: {str(e)}\n\nPlease check and try again."
                )
            except Exception as e:
                logger.error(f"Scan error: {e}")
                await update.message.reply_text(f"❌ Error: {str(e)}")
            
            del user_states[chat_id]

    # ==================== ADD HANDLERS ====================
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("scan", scan))
    telegram_app.add_handler(CommandHandler("cancel", cancel))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ==================== WEBHOOK HANDLER ====================
    async def webhook_handler(request):
        """Handle incoming Telegram updates via webhook"""
        try:
            data = await request.json()
            logger.info(f"📥 Webhook received: {data.get('update_id')}")
            
            update = Update.de_json(data, telegram_app.bot)
            await telegram_app.process_update(update)
            
            return web.Response(text="OK", status=200)
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.Response(text="OK", status=200)

    async def health_check(request):
        """Health check endpoint"""
        return web.Response(text="Bot is running!", status=200)

    # ==================== SETUP ====================
    async def setup_webhook():
        """Set the webhook on startup"""
        if RENDER_URL:
            webhook_url = f"{RENDER_URL}/webhook"
            try:
                await telegram_app.bot.delete_webhook()
                success = await telegram_app.bot.set_webhook(url=webhook_url)
                if success:
                    logger.info(f"✅ Webhook set to {webhook_url}")
                else:
                    logger.error("❌ Failed to set webhook")
            except Exception as e:
                logger.error(f"❌ Webhook setup error: {e}")

    async def main():
        """Main function to start the bot"""
        logger.info("🚀 Starting Solana Wallet Finder Bot...")
        logger.info(f"📊 Scanning {MAX_WALLETS} wallets")
        logger.info(f"🤖 Bot token exists: {bool(BOT_TOKEN)}")
        logger.info(f"🔐 pynacl installed: {HAS_NACL} - {'✅ Keys will work in Phantom' if HAS_NACL else '❌ Keys will NOT work in Phantom!'}")
        
        if not BOT_TOKEN:
            logger.error("❌ BOT_TOKEN not set!")
            return
        
        # Initialize the application
        await telegram_app.initialize()
        logger.info("✅ Application initialized")
        
        # Setup webhook
        await setup_webhook()
        
        # Setup aiohttp web server
        app = web.Application()
        app.router.add_post('/webhook', webhook_handler)
        app.router.add_get('/health', health_check)
        app.router.add_get('/', health_check)
        
        # Start web server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"✅ Web server started on port {PORT}")
        
        # Keep the application running
        await asyncio.Event().wait()

except Exception as e:
    print("="*60, file=sys.stderr)
    print("❌ CRITICAL STARTUP ERROR:", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    print("="*60, file=sys.stderr)
    sys.exit(1)

# ==================== RUN ====================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        print("="*60, file=sys.stderr)
        print("❌ RUNTIME ERROR:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("="*60, file=sys.stderr)
        sys.exit(1)
