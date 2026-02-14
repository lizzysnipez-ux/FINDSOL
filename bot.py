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
            text=f"🔍 Racing {len(RPC_ENDPOINTS)} RPC endpoints for maximum speed..."
        )
        
        found_count = 0
        reported_wallets = set()  # Track reported wallets
        batch_size = 20
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            for batch_start in range(0, MAX_WALLETS, batch_size):
                batch_end = min(batch_start + batch_size, MAX_WALLETS)
                batch = wallets[batch_start:batch_end]
                
                # Create racing tasks
                tasks = []
                for wallet in batch:
                    tasks.append(asyncio.gather(
                        check_balance_race(session, wallet['address']),
                        check_spl_tokens_race(session, wallet['address'])
                    ))
                
                results = await asyncio.gather(*tasks)
                
                # Process results
                for wallet, (sol_balance, spl_tokens) in zip(batch, results):
                    # Skip if already reported
                    if wallet['index'] in reported_wallets:
                        continue
                    
                    has_funds = sol_balance > 0 or len(spl_tokens) > 0
                    
                    if has_funds:
                        found_count += 1
                        reported_wallets.add(wallet['index'])
                        
                        # Build message
                        message = f"🎉 *WALLET WITH FUNDS FOUND!*\n\n"
                        message += f"📌 *Account Index:* `{wallet['index']}`\n"
                        message += f"📬 *Address:* `{wallet['address']}`\n"
                        message += f"💰 *SOL Balance:* `{sol_balance:.6f} SOL`\n"
                        
                        if spl_tokens:
                            message += f"\n🪙 *SPL Tokens:*\n"
                            for token in spl_tokens:
                                message += f"• *{token['symbol']}*: `{token['balance']}`\n"
                        
                        message += f"\n🔐 *BASE58 PRIVATE KEY:*\n"
                        message += f"`{wallet['private_key']['base58']}`"
                        
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=message,
                            parse_mode='Markdown'
                        )
                
                # Update progress (only every 20 wallets)
                elapsed = time.time() - start_time
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=f"📊 Progress: {batch_end}/{MAX_WALLETS} wallets... Found: {found_count} | Time: {elapsed:.0f}s"
                )
        
        # Final status
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
