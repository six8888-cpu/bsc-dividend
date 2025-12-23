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

# 多 RPC 节点，自动故障转移
RPC_URLS = [
    'https://bsc-dataseed.bnbchain.org',
    'https://bsc-dataseed1.binance.org',
    'https://bsc-dataseed2.binance.org',
    'https://bsc-dataseed3.binance.org',
    'https://bsc-dataseed4.binance.org',
    'https://bsc-rpc.publicnode.com',
    'https://bsc.meowrpc.com',
]
current_rpc_index = 0

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

# 初始化 Web3 连接
def create_web3(rpc_url):
    """创建 Web3 连接"""
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

def get_web3():
    """获取可用的 Web3 连接，自动故障转移"""
    global current_rpc_index, w3
    
    # 先测试当前连接
    try:
        if w3.is_connected():
            w3.eth.block_number  # 测试实际请求
            return w3
    except:
        pass
    
    # 当前连接失败，尝试其他节点
    for i in range(len(RPC_URLS)):
        idx = (current_rpc_index + i) % len(RPC_URLS)
        rpc_url = RPC_URLS[idx]
        try:
            new_w3 = create_web3(rpc_url)
            if new_w3.is_connected():
                new_w3.eth.block_number  # 测试实际请求
                if idx != current_rpc_index:
                    print(f"[RPC] 切换到: {rpc_url}")
                    current_rpc_index = idx
                    w3 = new_w3
                return w3
        except Exception as e:
            print(f"[RPC] {rpc_url} 不可用: {e}")
    
    print("[RPC] 警告: 所有节点都不可用!")
    return w3

# 初始化默认连接
w3 = create_web3(RPC_URLS[0])

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
    web3 = get_web3()
    return web3.from_wei(web3.eth.get_balance(address), 'ether')

def get_top_holders(contract_address):
    """获取代币前50持仓者地址"""
    web3 = get_web3()
    BALANCE_OF = '0x70a08231'
    all_addresses = set()
    contract_checksum = web3.to_checksum_address(contract_address)
    
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
            result = web3.eth.call({'to': contract_checksum, 'data': data})
            balance = int(result.hex(), 16)
            if balance >= 1000 * 10**18:
                holders.append((web3.to_checksum_address(addr), balance))
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

def send_dividend(config, amount_bnb, to_address, nonce=None, max_retries=3):
    """发送 BNB 分红，失败自动重试
    
    Args:
        nonce: 如果提供则使用指定的 nonce，否则从链上获取
    Returns:
        成功返回 (result_dict, used_nonce)，失败返回 (None, nonce)
    """
    web3 = get_web3()
    wallet = config['wallet_address']
    private_key = config['private_key']
    amount_wei = web3.to_wei(amount_bnb, 'ether')
    last_error = None
    
    # 如果没有提供 nonce，从链上获取
    if nonce is None:
        nonce = web3.eth.get_transaction_count(wallet, 'pending')
    
    for attempt in range(max_retries):
        try:
            # 每次重试前等待更长时间
            if attempt > 0:
                time.sleep(5 + attempt * 2)
                # 重试时重新获取 nonce 和 web3 连接（可能需要切换节点）
                web3 = get_web3()
                nonce = web3.eth.get_transaction_count(wallet, 'pending')
            
            # 每次重试增加 gas price 以加速确认
            gas_price_gwei = 3 + attempt * 2  # 3, 5, 7 gwei
            tx = {
                'from': wallet,
                'to': web3.to_checksum_address(to_address),
                'value': amount_wei,
                'gas': 21000,
                'gasPrice': web3.to_wei(gas_price_gwei, 'gwei'),
                'nonce': nonce,
                'chainId': 56
            }
            
            signed_tx = web3.eth.account.sign_transaction(tx, private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] != 1:
                last_error = "交易状态失败"
                continue
            
            tx_hash_str = tx_hash.hex()
            if not tx_hash_str.startswith('0x'):
                tx_hash_str = '0x' + tx_hash_str
            
            result = {
                'address': to_address[:6] + '...' + to_address[-4:],
                'full_address': to_address,
                'amount': amount_bnb,
                'tx_hash': tx_hash_str,
                'block': receipt['blockNumber'],
                'timestamp': int(time.time())
            }
            return (result, nonce + 1)  # 返回结果和下一个 nonce
        except Exception as e:
            last_error = str(e)
            print(f"分红重试 {attempt+1}/{max_retries} (nonce={nonce}, gas={gas_price_gwei}gwei): {e}")
    
    print(f"分红失败: 重试{max_retries}次后仍失败, 最后错误: {last_error}, 目标地址: {to_address}")
    return (None, nonce)

def buyback_and_burn(config, amount_bnb, max_retries=3):
    """通过 Flap Portal swapExactInput 回购代币并销毁，支持重试"""
    web3 = get_web3()
    wallet = web3.to_checksum_address(config['wallet_address'])
    private_key = config['private_key']
    contract_address = web3.to_checksum_address(config['contract_address'])
    
    token_contract = web3.eth.contract(address=contract_address, abi=ERC20_ABI)
    portal_contract = web3.eth.contract(address=web3.to_checksum_address(FLAP_PORTAL_ADDRESS), abi=FLAP_PORTAL_ABI)
    amount_wei = web3.to_wei(amount_bnb, 'ether')
    
    # ========== 第一步：购买代币（带重试）==========
    tokens_bought = 0
    buy_receipt = None
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(5 + attempt * 2)
                print(f"  购买重试 {attempt+1}/{max_retries}...")
            
            balance_before = token_contract.functions.balanceOf(wallet).call()
            print(f"  购买前余额: {balance_before / 1e18:,.2f} 枚")
            
            ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'
            swap_params = (
                ZERO_ADDRESS,      # inputToken: BNB
                contract_address,  # outputToken: 代币
                amount_wei,        # inputAmount
                0,                 # minOutputAmount: 0 表示接受任意数量
                b''                # permitData: 空
            )
            
            nonce = web3.eth.get_transaction_count(wallet, 'pending')
            gas_price_gwei = 3 + attempt * 2  # 3, 5, 7 gwei
            
            buy_tx = portal_contract.functions.swapExactInput(swap_params).build_transaction({
                'from': wallet,
                'value': amount_wei,
                'gas': 300000,
                'gasPrice': web3.to_wei(gas_price_gwei, 'gwei'),
                'nonce': nonce
            })
            
            signed_buy = web3.eth.account.sign_transaction(buy_tx, private_key)
            buy_hash = web3.eth.send_raw_transaction(signed_buy.raw_transaction)
            print(f"  购买交易: {buy_hash.hex()} (nonce={nonce}, gas={gas_price_gwei}gwei)")
            
            buy_receipt = web3.eth.wait_for_transaction_receipt(buy_hash, timeout=120)
            if buy_receipt['status'] != 1:
                print(f"  购买交易失败! status={buy_receipt['status']}")
                continue
            
            # 从交易日志中解析获得的代币数量
            TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            for log in buy_receipt['logs']:
                if log['address'].lower() == contract_address.lower() and log['topics'][0].hex() == TRANSFER_TOPIC:
                    to_addr = "0x" + log['topics'][2].hex()[-40:]
                    if to_addr.lower() == wallet.lower():
                        tokens_bought = int(log['data'].hex(), 16)
                        break
            
            if tokens_bought > 0:
                print(f"  购买成功: {tokens_bought / 1e18:,.2f} 枚")
                break
            else:
                print("  未获得代币，重试中...")
                
        except Exception as e:
            print(f"  购买异常 {attempt+1}/{max_retries}: {e}")
    
    if tokens_bought <= 0:
        print("  购买失败: 重试后仍未获得代币")
        return None
    
    # ========== 第二步：销毁代币（带重试）==========
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(5 + attempt * 2)
                print(f"  销毁重试 {attempt+1}/{max_retries}...")
            
            nonce = web3.eth.get_transaction_count(wallet, 'pending')
            gas_price_gwei = 3 + attempt * 2
            
            burn_tx = token_contract.functions.transfer(
                DEAD_ADDRESS, tokens_bought
            ).build_transaction({
                'from': wallet,
                'gas': 100000,
                'gasPrice': web3.to_wei(gas_price_gwei, 'gwei'),
                'nonce': nonce
            })
            
            signed_burn = web3.eth.account.sign_transaction(burn_tx, private_key)
            burn_hash = web3.eth.send_raw_transaction(signed_burn.raw_transaction)
            print(f"  销毁交易: {burn_hash.hex()} (nonce={nonce}, gas={gas_price_gwei}gwei)")
            
            receipt = web3.eth.wait_for_transaction_receipt(burn_hash, timeout=120)
            if receipt['status'] != 1:
                print(f"  销毁交易失败! status={receipt['status']}")
                continue
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
            print(f"  销毁异常 {attempt+1}/{max_retries}: {e}")
    
    print(f"  销毁失败: 重试后仍失败，代币可能留在钱包中")
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
        
        dividend_amount = available  # 100%用于分红，不回购
        
        state = load_state()
        result = {'timestamp': int(time.time())}
        
        # 分红：前30名均分
        print(f"\n分红: {dividend_amount:.6f} BNB")
        # 优先使用缓存数据，避免卡住
        holders = None
        if HOLDERS_FILE.exists():
            try:
                with open(HOLDERS_FILE) as f:
                    data = json.load(f)
                    holders = [(h['address'], h['balance']) for h in data.get('holders', [])[:30]]
                    print(f"  使用缓存持仓数据 ({len(holders)} 人)")
            except:
                pass
        if not holders:
            holders = get_top_holders(config['contract_address'])
            if holders:
                save_holders(holders, config['contract_address'])
        if not holders:
            print("  无法获取持仓者")
            return None
        
        min_dividend = 0.001  # 最小分红金额，低于此不发送（节省gas）
        dividend_results = []
        total_sent = 0
        
        # 前30名均分 100%
        top30 = holders[:30]
        if top30 and dividend_amount >= min_dividend:
            per_person = dividend_amount / len(top30)
            print(f"  [前30名均分] 总额: {dividend_amount:.6f} BNB, 每人: {per_person:.6f} BNB")
            
            # 获取初始 nonce，后续手动递增避免 nonce too low 错误
            web3 = get_web3()
            current_nonce = web3.eth.get_transaction_count(config['wallet_address'], 'pending')
            print(f"  初始 nonce: {current_nonce}")
            
            for i, (holder_addr, _) in enumerate(top30):
                if per_person < min_dividend:
                    continue
                div_result, current_nonce = send_dividend(config, per_person, holder_addr, nonce=current_nonce)
                if div_result:
                    dividend_results.append(div_result)
                    state['dividend'].insert(0, div_result)
                    total_sent += per_person
                    print(f"  [{i+1}/{len(top30)}] 发送成功: {holder_addr[:10]}... -> {per_person:.6f} BNB")
        
        state['dividend'] = state['dividend'][:100]
        result['dividend_count'] = len(dividend_results)
        result['dividend_total'] = total_sent
        print(f"  成功发送 {len(dividend_results)} 笔，共 {total_sent:.6f} BNB")
        
        # 保存状态
        state['last_block'] = get_web3().eth.block_number
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

last_holders_update = 0
holders_updating = False

def update_holders_cache():
    """更新持仓缓存（在单独线程中运行）"""
    global last_holders_update, holders_updating
    if holders_updating:
        return
    holders_updating = True
    try:
        config = load_config()
        if config:
            print(f"[{time.strftime('%H:%M:%S')}] 更新持仓缓存...")
            holders = get_top_holders(config['contract_address'])
            if holders:
                save_holders(holders, config['contract_address'])
                print(f"  已更新 {len(holders)} 个持仓者")
            last_holders_update = int(time.time())
    except Exception as e:
        print(f"更新持仓失败: {e}")
    finally:
        holders_updating = False

def background_scheduler():
    """后台定时任务"""
    global last_execution_time, last_holders_update
    print("后台调度器已启动")
    
    while True:
        current_time = int(time.time())
        
        # 每2分钟在后台线程更新持仓缓存（不阻塞分红）
        if current_time - last_holders_update >= 2 * 60 and not holders_updating:
            threading.Thread(target=update_holders_cache, daemon=True).start()
        
        remaining = get_countdown()
        
        # 倒计时归零时执行分红
        if remaining <= 1:
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
    
    # 获取最新分红结果
    last_result = None
    try:
        if RECORDS_FILE.exists():
            with open(RECORDS_FILE) as f:
                records = json.load(f)
                if records.get('dividend') and len(records['dividend']) > 0:
                    last_result = records['dividend'][0]
    except:
        pass
    
    return jsonify({
        'countdown': countdown,
        'running': lottery_running,
        'last_execution': last_execution_time,
        'last_result': last_result
    })

@app.route('/api/holders', methods=['GET'])
def api_holders():
    """获取持仓者列表（优先返回缓存，后台更新）"""
    try:
        # 优先返回缓存数据
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
