#!/usr/bin/env python3
import json
import time
import requests
from pathlib import Path

WALLET_ADDRESS = '0x6dad867551448dfad8775d4a2f78c12e200c6027'
CONTRACT_ADDRESS = '0x9bb72f4568157dad11a3f759ef4934bae1667777'
DEAD_ADDRESS = '0x000000000000000000000000000000000000dead'
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

OUTPUT_FILE = Path(__file__).parent / 'records.json'
STATE_FILE = Path(__file__).parent / 'state.json'

RPC_URL = 'https://bsc-dataseed.binance.org/'

def rpc_call(method, params):
    try:
        response = requests.post(RPC_URL, json={
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': 1
        }, timeout=15)
        result = response.json()
        return result.get('result')
    except Exception as e:
        print(f"RPC error: {e}")
        return None

def load_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {'last_block': 0, 'buyback': [], 'dividend': []}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def save_output(state):
    output = {
        'buyback': state['buyback'],
        'dividend': state['dividend'],
        'updated': int(time.time()),
        'last_block': state['last_block']
    }
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output, f)

def get_latest_block():
    result = rpc_call('eth_blockNumber', [])
    return int(result, 16) if result else 0

def check_tx_for_buyback(tx_hash):
    """检查交易是否包含回购销毁（Transfer到dead地址）"""
    receipt = rpc_call('eth_getTransactionReceipt', [tx_hash])
    if not receipt or not receipt.get('logs'):
        return None
    
    for log in receipt['logs']:
        # 检查是否是代币合约的Transfer事件
        if log.get('address', '').lower() != CONTRACT_ADDRESS.lower():
            continue
        
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        
        # 检查是Transfer事件且to是dead地址
        if topics[0].lower() == TRANSFER_TOPIC.lower():
            to_addr = '0x' + topics[2][26:].lower()
            if to_addr == DEAD_ADDRESS:
                amount = int(log['data'], 16) / 1e18
                return {
                    'amount': amount,
                    'tx_hash': tx_hash,
                    'block': int(receipt['blockNumber'], 16)
                }
    return None

def check_tx_for_dex_buyback(tx_hash):
    """检查交易是否通过DEX购买代币（钱包收到代币）"""
    receipt = rpc_call('eth_getTransactionReceipt', [tx_hash])
    if not receipt or not receipt.get('logs'):
        return None
    
    for log in receipt['logs']:
        # 检查是否是代币合约的Transfer事件
        if log.get('address', '').lower() != CONTRACT_ADDRESS.lower():
            continue
        
        topics = log.get('topics', [])
        if len(topics) < 3:
            continue
        
        # 检查是Transfer事件且to是钱包地址（钱包收到代币）
        if topics[0].lower() == TRANSFER_TOPIC.lower():
            to_addr = '0x' + topics[2][26:].lower()
            if to_addr == WALLET_ADDRESS.lower():
                amount = int(log['data'], 16) / 1e18
                return {
                    'amount': amount,
                    'tx_hash': tx_hash,
                    'block': int(receipt['blockNumber'], 16)
                }
    return None

def check_tx_for_dividend(tx):
    """检查交易是否是分红（BNB转账）"""
    value = int(tx.get('value', '0'), 16) / 1e18
    to_addr = tx.get('to', '')
    
    # BNB转账 > 0.01，且不是转到合约
    if value > 0.01 and to_addr and to_addr.lower() != CONTRACT_ADDRESS.lower():
        # 普通转账（input为空）
        if tx.get('input', '0x') in ['0x', '']:
            return {
                'address': to_addr[:6] + '...' + to_addr[-4:],
                'full_address': to_addr,
                'amount': value,
                'tx_hash': tx['hash'],
                'block': int(tx.get('blockNumber', '0'), 16)
            }
    return None

def scan_blocks(from_block, to_block, state):
    """扫描区块，查找钱包发出的交易"""
    for block_num in range(from_block, to_block + 1):
        block = rpc_call('eth_getBlockByNumber', [hex(block_num), True])
        if not block or not block.get('transactions'):
            continue
        
        for tx in block['transactions']:
            # 只检查钱包发出的交易
            if tx.get('from', '').lower() != WALLET_ADDRESS.lower():
                continue
            
            tx_hash = tx['hash']
            
            # 检查是否已记录
            if any(r['tx_hash'] == tx_hash for r in state['buyback']):
                continue
            if any(r['tx_hash'] == tx_hash for r in state['dividend']):
                continue
            
            # 检查是否是回购（调用合约的交易 或 通过DEX购买）
            to_addr = tx.get('to', '').lower()
            if to_addr == CONTRACT_ADDRESS.lower():
                # 直接调用合约的回购（burn到dead地址）
                buyback = check_tx_for_buyback(tx_hash)
                if buyback:
                    state['buyback'].insert(0, buyback)
                    print(f"New buyback (burn): {buyback['amount']:,.2f} tokens")
            elif to_addr and to_addr != CONTRACT_ADDRESS.lower():
                # 通过DEX购买代币的回购
                buyback = check_tx_for_dex_buyback(tx_hash)
                if buyback:
                    state['buyback'].insert(0, buyback)
                    print(f"New buyback (DEX): {buyback['amount']:,.2f} tokens")
            
            # 检查是否是分红
            dividend = check_tx_for_dividend(tx)
            if dividend:
                state['dividend'].insert(0, dividend)
                print(f"New dividend: {dividend['amount']:.4f} BNB to {dividend['address']}")

def main():
    print('Starting auto-monitor...')
    state = load_state()
    
    if state['last_block'] == 0:
        state['last_block'] = get_latest_block()
        print(f"Starting from block {state['last_block']}")
    
    save_output(state)
    
    while True:
        try:
            latest_block = get_latest_block()
            if latest_block == 0:
                time.sleep(5)
                continue
            
            from_block = state['last_block'] + 1
            to_block = latest_block
            
            if from_block <= to_block:
                # 限制每次最多扫描200个区块
                to_block = min(to_block, from_block + 200)
                
                print(f"Scanning blocks {from_block} to {to_block}...")
                scan_blocks(from_block, to_block, state)
                
                state['last_block'] = to_block
                state['buyback'] = state['buyback'][:50]
                state['dividend'] = state['dividend'][:50]
                
                save_state(state)
                save_output(state)
            
            print(f"[{time.strftime('%H:%M:%S')}] Block: {state['last_block']}, Buyback: {len(state['buyback'])}, Dividend: {len(state['dividend'])}")
            
        except Exception as e:
            print(f'Error: {e}')
            import traceback
            traceback.print_exc()
        
        time.sleep(5)

if __name__ == '__main__':
    main()
