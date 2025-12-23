#!/usr/bin/env python3
"""
BSC 分红 API 服务器
后端自动执行回购分红，前端只读取结果
"""
import json
import time
import random
import requests
import re
from pathlib import Path
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
import threading

app = Flask(__name__, static_folder='.')
CORS(app)

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / 'config.json'
STATE_FILE = BASE_DIR / 'state.json'
HOLDERS_FILE = BASE_DIR / 'holders.json'
RECORDS_FILE = BASE_DIR / 'records.json'

RPC_URL = 'https://bsc-dataseed.binance.org/'
DEAD_ADDRESS = '0x000000000000000000000000000000000000dEaD'
LP_POOL_ADDRESSES = [
    '0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0',
    '0x8892138836eb5ec0f9c9e810efd19d542cf566b8',
]
WBNB_ADDRESS = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
PANCAKE_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'

ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactETHForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]''')

# Flap Portal ABI for swapExactInput
FLAP_PORTAL_ABI = json.loads('''[
    {"inputs":[{"components":[{"name":"inputToken","type":"address"},{"name":"outputToken","type":"address"},{"name":"inputAmount","type":"uint256"},{"name":"minOutputAmount","type":"uint256"},{"name":"permitData","type":"bytes"}],"name":"params","type":"tuple"}],"name":"swapExactInput","outputs":[{"name":"outputAmount","type":"uint256"}],"stateMutability":"payable","type":"function"}
]''')

FLAP_PORTAL_ADDRESS = '0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0'

w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

# 全局状态
lottery_running = False
last_execution_time = 0

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
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

def save_records(state):
    output = {
        'buyback': state['buyback'],
        'dividend': state['dividend'],
        'updated': int(time.time()),
        'last_block': state['last_block']
    }
    with open(RECORDS_FILE, 'w') as f:
        json.dump(output, f)

def get_bnb_balance(address):
    return w3.from_wei(w3.eth.get_balance(address), 'ether')

def get_top_holders(contract_address):
    """获取代币前50持仓者地址"""
    BALANCE_OF = '0x70a08231'
    all_addresses = set()
    contract_checksum = w3.to_checksum_address(contract_address)
    
    # 获取多页数据以确保拿到前50名
    for page in range(1, 4):
        try:
            url = f"https://bscscan.com/token/generic-tokenholders2?a={contract_address}&s=0&p={page}"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                addresses = re.findall(r'0x[a-fA-F0-9]{40}', response.text)
                for addr in addresses:
                    all_addresses.add(addr.lower())
        except Exception as e:
            print(f"BSCScan获取失败: {e}")
    
    try:
        state = load_state()
        for div in state.get('dividend', []):
            addr = div.get('full_address', '')
            if addr:
                all_addresses.add(addr.lower())
    except:
        pass
    
    all_addresses.discard('0x0000000000000000000000000000000000000000')
    all_addresses.discard(DEAD_ADDRESS.lower())
    all_addresses.discard(contract_address.lower())
    for lp in LP_POOL_ADDRESSES:
        all_addresses.discard(lp.lower())
    
    if len(all_addresses) == 0:
        return []
    
    holders = []
    for addr in all_addresses:
        try:
            padded_addr = addr[2:].zfill(64)
            data = BALANCE_OF + padded_addr
            result = w3.eth.call({'to': contract_checksum, 'data': data})
            balance = int(result.hex(), 16)
            if balance >= 1000 * 10**18:
                holders.append((w3.to_checksum_address(addr), balance))
        except:
            pass
    
    holders.sort(key=lambda x: x[1], reverse=True)
    return holders[:100]

def save_holders(holders, contract_address):
    holders_data = [{'address': addr, 'balance': bal / 1e18} for addr, bal in holders]
    output = {
        'holders': holders_data,
        'updated': int(time.time()),
        'contract': contract_address
    }
    with open(HOLDERS_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    return output

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
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] != 1:
            return None
        
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
        print(f"分红失败: {e}")
        return None

def buyback_and_burn(config, amount_bnb):
    """通过 Flap Portal swapExactInput 回购代币并销毁"""
    try:
        wallet = w3.to_checksum_address(config['wallet_address'])
        private_key = config['private_key']
        contract_address = w3.to_checksum_address(config['contract_address'])
        
        token_contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
        portal_contract = w3.eth.contract(address=w3.to_checksum_address(FLAP_PORTAL_ADDRESS), abi=FLAP_PORTAL_ABI)
        amount_wei = w3.to_wei(amount_bnb, 'ether')
        
        # 记录购买前余额
        balance_before = token_contract.functions.balanceOf(wallet).call()
        print(f"  购买前余额: {balance_before / 1e18:,.2f} 枚")
        
        # 使用 Flap Portal swapExactInput 购买代币
        # inputToken = 0x0 (BNB), outputToken = 代币地址
        ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'
        swap_params = (
            ZERO_ADDRESS,      # inputToken: BNB
            contract_address,  # outputToken: 代币
            amount_wei,        # inputAmount
            0,                 # minOutputAmount: 0 表示接受任意数量
            b''                # permitData: 空
        )
        
        nonce = w3.eth.get_transaction_count(wallet)
        buy_tx = portal_contract.functions.swapExactInput(swap_params).build_transaction({
            'from': wallet,
            'value': amount_wei,
            'gas': 300000,
            'gasPrice': w3.to_wei('3', 'gwei'),
            'nonce': nonce
        })
        
        signed_buy = w3.eth.account.sign_transaction(buy_tx, private_key)
        buy_hash = w3.eth.send_raw_transaction(signed_buy.raw_transaction)
        print(f"  购买交易: {buy_hash.hex()}")
        
        receipt = w3.eth.wait_for_transaction_receipt(buy_hash, timeout=120)
        if receipt['status'] != 1:
            print("  购买交易失败!")
            return None
        
        # 从交易日志中解析获得的代币数量 (避免 RPC 缓存问题)
        TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        tokens_bought = 0
        for log in receipt['logs']:
            if log['address'].lower() == contract_address.lower() and log['topics'][0].hex() == TRANSFER_TOPIC:
                to_addr = "0x" + log['topics'][2].hex()[-40:]
                if to_addr.lower() == wallet.lower():
                    tokens_bought = int(log['data'].hex(), 16)
                    break
        
        print(f"  购买成功: {tokens_bought / 1e18:,.2f} 枚")
        
        # 只销毁本次购买的代币
        if tokens_bought <= 0:
            print("  未获得代币!")
            return None
            
        nonce = w3.eth.get_transaction_count(wallet)
        burn_tx = token_contract.functions.transfer(
            DEAD_ADDRESS, tokens_bought
        ).build_transaction({
            'from': wallet,
            'gas': 100000,
            'gasPrice': w3.to_wei('3', 'gwei'),
            'nonce': nonce
        })
        
        signed_burn = w3.eth.account.sign_transaction(burn_tx, private_key)
        burn_hash = w3.eth.send_raw_transaction(signed_burn.raw_transaction)
        print(f"  销毁交易: {burn_hash.hex()}")
        
        receipt = w3.eth.wait_for_transaction_receipt(burn_hash, timeout=120)
        if receipt['status'] != 1:
            return None
        
        tx_hash_str = burn_hash.hex()
        if not tx_hash_str.startswith('0x'):
            tx_hash_str = '0x' + tx_hash_str
        
        return {
            'amount': tokens_bought / 1e18,
            'tx_hash': tx_hash_str,
            'block': receipt['blockNumber'],
            'bnb_spent': amount_bnb,
            'timestamp': int(time.time())
        }
    except Exception as e:
        print(f"回购销毁失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def execute_lottery():
    """执行一轮回购分红"""
    global lottery_running, last_execution_time
    
    if lottery_running:
        return None
    
    lottery_running = True
    print(f"\n{'='*50}")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始执行回购分红")
    print(f"{'='*50}")
    
    try:
        config = load_config()
        if not config:
            print("错误: 配置文件不存在")
            return None
        
        balance = float(get_bnb_balance(config['wallet_address']))
        gas_reserve = 0.002  # 只预留 gas 费用
        available = balance - gas_reserve
        
        print(f"当前余额: {balance:.6f} BNB, 可用: {available:.6f} BNB")
        
        if available <= 0:
            print(f"余额不足以支付 gas，跳过本轮")
            return None
        
        buyback_amount = available * 0.5
        dividend_amount = available * 0.5
        
        state = load_state()
        result = {'timestamp': int(time.time())}
        
        # 1. 回购销毁
        print(f"\n[1/2] 回购销毁: {buyback_amount:.6f} BNB")
        buyback_result = buyback_and_burn(config, buyback_amount)
        if buyback_result:
            state['buyback'].insert(0, buyback_result)
            state['buyback'] = state['buyback'][:50]
            result['buyback'] = buyback_result
            print(f"  成功销毁 {buyback_result['amount']:,.2f} 枚")
        
        # 2. 分红：前20名均分(20%) + 21-100名随机一人(30%)
        print(f"\n[2/2] 分红: {dividend_amount:.6f} BNB")
        holders = get_top_holders(config['contract_address'])
        if not holders:
            print("  无法获取持仓者")
            return None
        
        save_holders(holders, config['contract_address'])
        
        min_dividend = 0.001  # 最小分红金额，低于此不发送（节省gas）
        dividend_results = []
        total_sent = 0
        
        # 前20名均分 40%
        top20 = holders[:20]
        top20_amount = dividend_amount * 0.4
        if top20 and top20_amount >= min_dividend:
            per_person = top20_amount / len(top20)
            print(f"  [前20名均分] 总额: {top20_amount:.6f} BNB, 每人: {per_person:.6f} BNB")
            for holder_addr, _ in top20:
                if per_person < min_dividend:
                    continue
                div_result = send_dividend(config, per_person, holder_addr)
                if div_result:
                    dividend_results.append(div_result)
                    state['dividend'].insert(0, div_result)
                    total_sent += per_person
        
        # 前100名随机抽30人分 60%（前20也参与）
        random_total = dividend_amount * 0.6
        pool = holders[:100]
        if pool and random_total >= min_dividend:
            winners = random.sample(pool, min(30, len(pool)))
            per_winner = random_total / len(winners)
            print(f"  [前100名随机30人] 总额: {random_total:.6f} BNB, 每人: {per_winner:.6f} BNB")
            for winner_addr, _ in winners:
                if per_winner < min_dividend:
                    continue
                div_result = send_dividend(config, per_winner, winner_addr)
                if div_result:
                    dividend_results.append(div_result)
                    state['dividend'].insert(0, div_result)
                    total_sent += per_winner
        
        state['dividend'] = state['dividend'][:100]
        result['dividend_count'] = len(dividend_results)
        result['dividend_total'] = total_sent
        print(f"  成功发送 {len(dividend_results)} 笔，共 {total_sent:.6f} BNB")
        
        # 保存状态
        state['last_block'] = w3.eth.block_number
        save_state(state)
        save_records(state)
        
        last_execution_time = int(time.time())
        print(f"\n本轮执行完成!")
        return result
        
    except Exception as e:
        print(f"执行失败: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        lottery_running = False

def get_countdown():
    """获取距离下次执行的倒计时（秒）"""
    now = time.localtime()
    minutes = now.tm_min % 5
    seconds = now.tm_sec
    remaining = (4 - minutes) * 60 + (59 - seconds)
    return remaining

def background_scheduler():
    """后台定时任务"""
    global last_execution_time
    print("后台调度器已启动")
    
    while True:
        remaining = get_countdown()
        
        # 倒计时归零时执行
        if remaining <= 1:
            current_time = int(time.time())
            # 防止重复执行（冷却4分钟）
            if current_time - last_execution_time >= 4 * 60:
                execute_lottery()
            time.sleep(5)
        else:
            time.sleep(1)

# ========== Flask 路由 (只读) ==========

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('.', filename)

@app.route('/api/status', methods=['GET'])
def api_status():
    """获取当前状态和倒计时"""
    countdown = get_countdown()
    
    return jsonify({
        'countdown': countdown,
        'running': lottery_running,
        'last_execution': last_execution_time
    })

@app.route('/api/holders', methods=['GET'])
def api_holders():
    """获取持仓者列表（实时从链上获取）"""
    try:
        config = load_config()
        if not config:
            # 无配置时返回缓存
            if HOLDERS_FILE.exists():
                with open(HOLDERS_FILE) as f:
                    return jsonify(json.load(f))
            return jsonify({'holders': [], 'updated': 0})
        
        # 从链上获取最新持仓者
        holders = get_top_holders(config['contract_address'])
        if holders:
            result = save_holders(holders, config['contract_address'])
            return jsonify(result)
        
        # 获取失败时返回缓存
        if HOLDERS_FILE.exists():
            with open(HOLDERS_FILE) as f:
                return jsonify(json.load(f))
        return jsonify({'holders': [], 'updated': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/records', methods=['GET'])
def api_records():
    """获取历史记录"""
    try:
        if RECORDS_FILE.exists():
            with open(RECORDS_FILE) as f:
                return jsonify(json.load(f))
        return jsonify({'buyback': [], 'dividend': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("=" * 50)
    print("BSC 分红系统 (纯后端执行)")
    print("=" * 50)
    
    config = load_config()
    if config:
        print(f"钱包地址: {config['wallet_address']}")
        print(f"合约地址: {config['contract_address']}")
    else:
        print("警告: 配置文件不存在")
    
    print(f"\n当前倒计时: {get_countdown()} 秒")
    print("后端每5分钟自动执行回购+分红")
    print("\n服务器启动在 http://0.0.0.0:5000")
    
    # 启动后台调度器
    scheduler_thread = threading.Thread(target=background_scheduler, daemon=True)
    scheduler_thread.start()
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
