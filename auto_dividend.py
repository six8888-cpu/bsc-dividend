#!/usr/bin/env python3
"""
BSC 自动回购分红脚本
- 每20分钟执行一次
- 50% BNB 用于回购代币并销毁
- 50% BNB 随机分红给Top20持仓者之一
"""
import json
import time
import random
import requests
import getpass
from pathlib import Path
from web3 import Web3
from eth_account import Account

CONFIG_FILE = Path(__file__).parent / 'config.json'
OUTPUT_FILE = Path(__file__).parent / 'records.json'
STATE_FILE = Path(__file__).parent / 'state.json'
LOTTERY_FILE = Path(__file__).parent / 'lottery.json'
HOLDERS_FILE = Path(__file__).parent / 'holders.json'

RPC_URL = 'https://bsc-dataseed.binance.org/'
BSCSCAN_API = 'https://api.bscscan.com/api'
DEAD_ADDRESS = '0x000000000000000000000000000000000000dEaD'
# Flap.sh LP池子地址，排除分红
LP_POOL_ADDRESSES = [
    '0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0',
    '0x8892138836eb5ec0f9c9e810efd19d542cf566b8',
]
WBNB_ADDRESS = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
PANCAKE_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'

# PancakeSwap Router ABI (只需要 swap 函数)
ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]''')

# ERC20 ABI (只需要 transfer 和 balanceOf)
ERC20_ABI = json.loads('''[
    {"inputs":[{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":""}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":""}],"stateMutability":"view","type":"function"}
]''')

w3 = Web3(Web3.HTTPProvider(RPC_URL))
# BSC是POA链，需要添加中间件
from web3.middleware import ExtraDataToPOAMiddleware
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return None

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"配置已保存到 {CONFIG_FILE}")

def setup_config():
    print("\n=== BSC 自动回购分红配置 ===\n")
    
    wallet_address = input("请输入您的钱包地址: ").strip()
    if not w3.is_address(wallet_address):
        print("错误: 无效的钱包地址")
        return None
    wallet_address = w3.to_checksum_address(wallet_address)
    
    private_key = getpass.getpass("请输入您的私钥 (不会显示): ").strip()
    if not private_key.startswith('0x'):
        private_key = '0x' + private_key
    
    try:
        account = Account.from_key(private_key)
        if account.address.lower() != wallet_address.lower():
            print("错误: 私钥与钱包地址不匹配")
            return None
    except Exception as e:
        print(f"错误: 无效的私钥 - {e}")
        return None
    
    contract_address = input("请输入代币合约地址: ").strip()
    if not w3.is_address(contract_address):
        print("错误: 无效的合约地址")
        return None
    contract_address = w3.to_checksum_address(contract_address)
    
    bscscan_api_key = input("请输入 BSCScan API Key (可选，回车跳过): ").strip()
    
    min_balance = input("最小执行余额 BNB (默认 0.01): ").strip()
    min_balance = float(min_balance) if min_balance else 0.01
    
    config = {
        'wallet_address': wallet_address,
        'private_key': private_key,
        'contract_address': contract_address,
        'bscscan_api_key': bscscan_api_key,
        'min_balance': min_balance,
        'interval_minutes': 20
    }
    
    save_config(config)
    return config

def get_bnb_balance(address):
    return w3.from_wei(w3.eth.get_balance(address), 'ether')

def get_token_balance(contract_address, wallet_address):
    contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
    return contract.functions.balanceOf(wallet_address).call()

def get_top_holders(contract_address, api_key=''):
    """获取代币前20持仓地址 - 通过爬取BSCScan获取"""
    import re
    
    BALANCE_OF = '0x70a08231'
    all_addresses = set()
    contract_checksum = w3.to_checksum_address(contract_address)
    
    # 1. 从BSCScan爬取holders列表
    print("  从BSCScan获取持仓者列表...")
    try:
        url = f"https://bscscan.com/token/generic-tokenholders2?a={contract_address}&s=0&p=1"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            addresses = re.findall(r'0x[a-fA-F0-9]{40}', response.text)
            for addr in addresses:
                all_addresses.add(addr.lower())
            print(f"    从BSCScan获取 {len(all_addresses)} 个地址")
    except Exception as e:
        print(f"    BSCScan获取失败: {e}")
    
    # 2. 补充历史分红记录中的地址
    try:
        state = load_state()
        for div in state.get('dividend', []):
            addr = div.get('full_address', '')
            if addr:
                all_addresses.add(addr.lower())
    except:
        pass
    
    # 移除无效地址
    all_addresses.discard('0x0000000000000000000000000000000000000000')
    all_addresses.discard(DEAD_ADDRESS.lower())
    all_addresses.discard(contract_address.lower())
    for lp in LP_POOL_ADDRESSES:
        all_addresses.discard(lp.lower())  # 排除LP池子
    
    if len(all_addresses) == 0:
        print("  未找到任何持仓者地址")
        return []
    
    print(f"  查询 {len(all_addresses)} 个地址的余额...")
    
    # 查询每个地址的余额
    holders = []
    for addr in all_addresses:
        try:
            padded_addr = addr[2:].zfill(64)
            data = BALANCE_OF + padded_addr
            
            result = w3.eth.call({
                'to': contract_checksum,
                'data': data
            })
            
            balance = int(result.hex(), 16)
            # 最小持仓 1000 枚才参与分红
            if balance >= 1000 * 10**18:
                holders.append((w3.to_checksum_address(addr), balance))
        except:
            pass
    
    # 按余额排序
    holders.sort(key=lambda x: x[1], reverse=True)
    
    # 返回前20个地址
    result = [addr for addr, bal in holders[:20]]
    print(f"  找到 {len(result)} 个有效持仓者")
    
    if result:
        for i, addr in enumerate(result):
            bal = next((b for a, b in holders if a == addr), 0)
            print(f"    {i+1}. {addr[:6]}...{addr[-4:]} - {bal/1e18:,.2f} 枚")
    
    return result

def buyback_and_burn(config, amount_bnb):
    """通过 PancakeSwap 回购代币并销毁"""
    try:
        wallet = config['wallet_address']
        private_key = config['private_key']
        contract_address = config['contract_address']
        
        router = w3.eth.contract(address=PANCAKE_ROUTER, abi=ROUTER_ABI)
        token_contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
        
        amount_wei = w3.to_wei(amount_bnb, 'ether')
        path = [WBNB_ADDRESS, contract_address]
        deadline = int(time.time()) + 300
        
        # 获取预期输出
        amounts_out = router.functions.getAmountsOut(amount_wei, path).call()
        min_tokens = int(amounts_out[1] * 0.9)  # 10% 滑点
        
        print(f"  预计获得代币: {amounts_out[1] / 1e18:,.2f}")
        
        # 1. 先在DEX购买代币到钱包
        nonce = w3.eth.get_transaction_count(wallet)
        swap_tx = router.functions.swapExactETHForTokens(
            min_tokens,
            path,
            wallet,
            deadline
        ).build_transaction({
            'from': wallet,
            'value': amount_wei,
            'gas': 300000,
            'gasPrice': w3.to_wei('3', 'gwei'),
            'nonce': nonce
        })
        
        signed_swap = w3.eth.account.sign_transaction(swap_tx, private_key)
        swap_hash = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
        print(f"  购买交易已发送: {swap_hash.hex()}")
        
        # 等待购买完成
        receipt = w3.eth.wait_for_transaction_receipt(swap_hash, timeout=120)
        if receipt['status'] != 1:
            print("  购买交易失败!")
            return None
        
        # 获取实际购买的代币数量
        token_balance = token_contract.functions.balanceOf(wallet).call()
        print(f"  购买成功! 钱包代币余额: {token_balance / 1e18:,.2f}")
        
        # 2. 转到黑洞地址销毁
        nonce = w3.eth.get_transaction_count(wallet)
        burn_tx = token_contract.functions.transfer(
            DEAD_ADDRESS,
            token_balance
        ).build_transaction({
            'from': wallet,
            'gas': 100000,
            'gasPrice': w3.to_wei('3', 'gwei'),
            'nonce': nonce
        })
        
        signed_burn = w3.eth.account.sign_transaction(burn_tx, private_key)
        burn_hash = w3.eth.send_raw_transaction(signed_burn.raw_transaction)
        print(f"  销毁交易已发送: {burn_hash.hex()}")
        
        receipt = w3.eth.wait_for_transaction_receipt(burn_hash, timeout=120)
        if receipt['status'] != 1:
            print("  销毁交易失败!")
            return None
        
        print(f"  销毁成功! 已销毁 {token_balance / 1e18:,.2f} 代币")
        
        tx_hash_str = burn_hash.hex()
        if not tx_hash_str.startswith('0x'):
            tx_hash_str = '0x' + tx_hash_str
        
        return {
            'amount': token_balance / 1e18,
            'tx_hash': tx_hash_str,
            'block': receipt['blockNumber'],
            'bnb_spent': amount_bnb,
            'timestamp': int(time.time())
        }
        
    except Exception as e:
        print(f"  回购销毁失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def send_dividend(config, amount_bnb, to_address):
    """发送 BNB 分红"""
    try:
        wallet = config['wallet_address']
        private_key = config['private_key']
        
        amount_wei = w3.to_wei(amount_bnb, 'ether')
        
        nonce = w3.eth.get_transaction_count(wallet)
        tx = {
            'from': wallet,
            'to': w3.to_checksum_address(to_address),
            'value': amount_wei,
            'gas': 21000,
            'gasPrice': w3.to_wei('3', 'gwei'),
            'nonce': nonce
        }
        
        signed_tx = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"  分红交易已发送: {tx_hash.hex()}")
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt['status'] != 1:
            print("  分红交易失败!")
            return None
        
        print(f"  分红成功! 已转账 {amount_bnb:.6f} BNB 到 {to_address[:10]}...")
        
        tx_hash_str = tx_hash.hex()
        if not tx_hash_str.startswith('0x'):
            tx_hash_str = '0x' + tx_hash_str
        
        return {
            'address': to_address[:6] + '...' + to_address[-4:],
            'full_address': to_address,
            'amount': amount_bnb,
            'tx_hash': tx_hash_str,
            'block': receipt['blockNumber'],
            'timestamp': int(time.time())
        }
        
    except Exception as e:
        print(f"  分红失败: {e}")
        return None

def save_lottery_result(holders, winner_index, winner_address, amount):
    """保存抽奖结果供网页显示"""
    lottery = {
        'timestamp': int(time.time()),
        'candidates': [{'address': h[:6] + '...' + h[-4:], 'full_address': h} for h in holders],
        'winner_index': winner_index,
        'winner_address': winner_address[:6] + '...' + winner_address[-4:],
        'winner_full_address': winner_address,
        'amount': amount,
        'status': 'completed'
    }
    with open(LOTTERY_FILE, 'w') as f:
        json.dump(lottery, f, indent=2)
    print(f"  抽奖结果已保存")

def save_holders(holders, contract_address):
    """保存持仓者列表供网页显示"""
    BALANCE_OF = '0x70a08231'
    contract_checksum = w3.to_checksum_address(contract_address)
    
    holders_data = []
    for addr in holders:
        try:
            padded = addr[2:].lower().zfill(64)
            data = BALANCE_OF + padded
            result = w3.eth.call({'to': contract_checksum, 'data': data})
            balance = int(result.hex(), 16) / 1e18
            holders_data.append({'address': addr, 'balance': balance})
        except:
            pass
    
    output = {
        'holders': holders_data,
        'updated': int(time.time()),
        'contract': contract_address
    }
    with open(HOLDERS_FILE, 'w') as f:
        json.dump(output, f, indent=2)

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

def execute_round(config):
    """执行一轮回购分红"""
    print(f"\n{'='*50}")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始执行回购分红")
    print(f"{'='*50}")
    
    state = load_state()
    
    # 检查余额
    balance = get_bnb_balance(config['wallet_address'])
    print(f"当前 BNB 余额: {balance:.6f}")
    
    # 预留 gas 费用
    gas_reserve = 0.005
    available = float(balance) - gas_reserve
    
    if available < config['min_balance']:
        print(f"余额不足 (最小: {config['min_balance']} BNB)，跳过本轮")
        return
    
    # 50% 回购，50% 分红
    buyback_amount = available / 2
    dividend_amount = available / 2
    
    print(f"回购金额: {buyback_amount:.6f} BNB")
    print(f"分红金额: {dividend_amount:.6f} BNB")
    
    # 1. 执行回购销毁
    print(f"\n[1/2] 执行回购销毁...")
    buyback_result = buyback_and_burn(config, buyback_amount)
    if buyback_result:
        state['buyback'].insert(0, buyback_result)
        state['buyback'] = state['buyback'][:50]
    
    # 2. 获取持仓者并抽奖
    print(f"\n[2/2] 执行分红抽奖...")
    holders = get_top_holders(config['contract_address'], config.get('bscscan_api_key', ''))
    
    # 更新网站显示的持仓者列表
    if holders:
        save_holders(holders, config['contract_address'])
        print(f"  持仓者列表已更新")
    
    if not holders:
        print("  无法获取持仓者列表，跳过分红")
    else:
        print(f"  获取到 {len(holders)} 个持仓者")
        for i, h in enumerate(holders):
            print(f"    {i+1}. {h[:6]}...{h[-4:]}")
        
        # 随机抽取
        winner_index = random.randint(0, len(holders) - 1)
        winner = holders[winner_index]
        print(f"\n  抽奖结果: 第 {winner_index + 1} 位 - {winner[:10]}...")
        
        # 保存抽奖结果
        save_lottery_result(holders, winner_index, winner, dividend_amount)
        
        # 发送分红
        dividend_result = send_dividend(config, dividend_amount, winner)
        if dividend_result:
            state['dividend'].insert(0, dividend_result)
            state['dividend'] = state['dividend'][:50]
    
    # 保存状态
    state['last_block'] = w3.eth.block_number
    save_state(state)
    save_output(state)
    
    print(f"\n本轮执行完成!")
    print(f"回购记录: {len(state['buyback'])} 条")
    print(f"分红记录: {len(state['dividend'])} 条")

def main():
    print("=" * 50)
    print("BSC 自动回购分红系统")
    print("=" * 50)
    
    # 加载或创建配置
    config = load_config()
    if not config:
        config = setup_config()
        if not config:
            return
    else:
        print(f"\n已加载配置:")
        print(f"  钱包地址: {config['wallet_address']}")
        print(f"  合约地址: {config['contract_address']}")
        print(f"  执行间隔: {config['interval_minutes']} 分钟")
        
        choice = input("\n是否重新配置? (y/N): ").strip().lower()
        if choice == 'y':
            config = setup_config()
            if not config:
                return
    
    # 检查连接
    if not w3.is_connected():
        print("错误: 无法连接到 BSC 网络")
        return
    
    print(f"\n已连接到 BSC 网络")
    print(f"当前区块: {w3.eth.block_number}")
    
    balance = get_bnb_balance(config['wallet_address'])
    print(f"钱包 BNB 余额: {balance:.6f}")
    
    interval = config['interval_minutes'] * 60
    
    print(f"\n开始自动执行，间隔 {config['interval_minutes']} 分钟")
    print("按 Ctrl+C 停止\n")
    
    # 立即执行一次
    execute_round(config)
    
    # 循环执行
    while True:
        print(f"\n等待 {config['interval_minutes']} 分钟后执行下一轮...")
        time.sleep(interval)
        execute_round(config)

if __name__ == '__main__':
    main()
