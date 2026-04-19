import os
import sys
import time
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog
from datetime import datetime

# ==========================================
# 0. CRITICAL FIX FOR PYINSTALLER --WINDOWED
# ==========================================
log_path = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__), "timm_system.log")
log_file = open(log_path, "w", buffering=1)
sys.stdout = log_file
sys.stderr = log_file

print("--- TIMM SYSTEM LOG BOOTING ---")

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import webview

# ==========================================
# 1. SERVER INITIALIZATION
# ==========================================

if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

# ==========================================
# 2. GLOBAL HARDWARE STATES & BUFFERS
# ==========================================

clock_running = False
clock_delay = 1.0
keep_running = False
auto_run_thread = None
auto_run_lock = threading.Lock()

main_memory = [0] * 16
hard_disk_buffer = {}
loaded_file_path = ""

printer_buffer = []
printer_has_jobs = False
network_rx_buffer = []
network_logs = []

cpu_waiting_for_input = False
active_in_port = 0
accumulator = 0

# FIX: Added clock_cycles_remaining + temporal_mode fields
cpu_state = {
    "PC": 0, "AC": 0, "IR": 0, "CAR": "00000", "cycles": 0,
    "clock_cycles_remaining": 0,
    "mode": "IDLE", "MPO_decision": False, "THB": {},
    "is_halted": False, "fgi_flag": False, "input_buffer": 0,
    "active_path": [], "active_components": [],
    "temporal_mode_active": False,
    "temporal_mode_instruction": ""
}

# ==========================================
# 3. BACKGROUND THREADS
# ==========================================

def clock_loop():
    global clock_running, clock_delay, cpu_waiting_for_input
    cycle_count = 0
    while True:
        if clock_running and not cpu_waiting_for_input:
            cycle_count += 1
            socketio.emit('tick', {'cycle': cycle_count})
        time.sleep(clock_delay)

thread = threading.Thread(target=clock_loop, daemon=True)
thread.start()

def auto_run_loop():
    global keep_running, cpu_state, clock_delay, cpu_waiting_for_input
    while keep_running:
        if cpu_state["is_halted"]:
            keep_running = False
            socketio.emit('execution_stopped', {'reason': 'HALTED'})
            break
        if cpu_waiting_for_input:
            time.sleep(0.05)
            continue
        with auto_run_lock:
            _do_step()
        time.sleep(clock_delay)

# ==========================================
# 4. ASSEMBLER & DISASSEMBLER
# ==========================================

def assemble(text):
    text = text.strip().upper()
    if len(text) == 8 and all(c in '01' for c in text):
        return int(text, 2)
    if text.startswith('DEC '):
        try:
            val = int(text.split(' ')[1])
            if val < 0: val = (256 + val)
            return val & 0xFF
        except: pass
    if text.startswith('HEX '):
        try: return int(text.split(' ')[1], 16) & 0xFF
        except: pass
    # FIX: Support bare hex values (e.g. "A3", "FF")
    if len(text) <= 2:
        try: return int(text, 16) & 0xFF
        except: pass

    impl_map = {"NOP": 0x00, "HLT": 0xF0, "CLR": 0xE0, "SHL": 0xE1, "SHR": 0xE2,
                "INP": 0xE3, "OUT": 0xE4, "ION": 0xE5, "IOF": 0xE6}
    if text in impl_map: return impl_map[text]

    op_map = {1: "LDA", 2: "STA", 3: "ADD", 4: "SUB", 5: "MVI", 6: "ADI",
              7: "JMP", 8: "JNZ", 9: "AND", 0xA: "OR", 0xB: "MOV", 0xC: "INC", 0xD: "DEC"}
    reverse_op_map = {v: k for k, v in op_map.items()}

    parts = text.split()
    if len(parts) >= 2 and parts[0] in reverse_op_map:
        opcode = reverse_op_map[parts[0]]
        operand_str = parts[1]
        if operand_str == "[R2]": operand = 0xF
        else:
            try: operand = int(operand_str, 16) & 0xF
            except: operand = 0
        return (opcode << 4) | operand
    # FIX: Decimal integer fallback
    try: return int(text) & 0xFF
    except: pass
    return 0

def disassemble(val):
    impl_map = {0x00: "NOP", 0xF0: "HLT", 0xE0: "CLR", 0xE1: "SHL", 0xE2: "SHR",
                0xE3: "INP", 0xE4: "OUT", 0xE5: "ION", 0xE6: "IOF"}
    if val in impl_map: return impl_map[val]

    high = (val >> 4) & 0xF
    low = val & 0xF

    op_map = {1: "LDA", 2: "STA", 3: "ADD", 4: "SUB", 5: "MVI", 6: "ADI",
              7: "JMP", 8: "JNZ", 9: "AND", 0xA: "OR", 0xB: "MOV", 0xC: "INC", 0xD: "DEC"}
    if high in op_map:
        op_name = op_map[high]
        operand_str = "[R2]" if low == 0xF else f"{low:X}"
        return f"{op_name} {operand_str}"
    return f"DATA {val:02X}"

def basic_tbt_assembler(filepath):
    global hard_disk_buffer
    hard_disk_buffer = {}
    current_address = 0
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith(';'): continue
            instruction = line
            if ',' in instruction: instruction = instruction.split(',')[1].strip()
            if ';' in instruction: instruction = instruction.split(';')[0].strip()
            if not instruction: continue
            if instruction.upper().startswith('ORG'):
                addr_str = instruction.split()[1]
                try:
                    current_address = int(addr_str, 16) if any(c in addr_str.upper() for c in 'ABCDEF') else int(addr_str)
                except: current_address = 0
                continue
            if instruction.upper().startswith('END'): break
            assembled_val = assemble(instruction)
            hard_disk_buffer[current_address] = {
                'text': instruction,
                'hex': f"{assembled_val:02X}"
            }
            current_address += 1
        return True
    except Exception as e:
        print(f"Hard Disk Read Error: {e}")
        return False

# ==========================================
# 5. CPU EXECUTION LOGIC
# ==========================================

def format_cpu_response():
    global cpu_state, main_memory
    curr_ir = cpu_state["IR"]
    next_pc = cpu_state["PC"] & 0xF
    next_ir = main_memory[next_pc]
    return {
        "PC": f"{cpu_state['PC']:01X}",
        "AC": f"{cpu_state['AC']:02X}",
        "IR": f"{cpu_state['IR']:02X}",
        "CAR": cpu_state["CAR"],
        "cycles": cpu_state["cycles"],
        "clock_cycles_remaining": cpu_state["clock_cycles_remaining"],
        "mode": cpu_state["mode"],
        "temporal_mode_active": cpu_state["temporal_mode_active"],
        "temporal_mode_instruction": cpu_state["temporal_mode_instruction"],
        "current_instruction": disassemble(curr_ir),
        "next_instruction": disassemble(next_ir),
        "MPO_decision": cpu_state["MPO_decision"],
        "THB": cpu_state["THB"],
        "active_path": cpu_state["active_path"],
        "active_components": cpu_state["active_components"],
        "is_halted": cpu_state["is_halted"],
        "memory": [f"{v:02X}" for v in main_memory],
        "memory_bin": [f"{v:08b}" for v in main_memory],
        "memory_mnem": [disassemble(v) for v in main_memory],
    }

def execute_out_instruction(port, ac_value):
    global printer_buffer, network_logs
    if port == 1:
        print(f"OUT 1 (Display): Sending {ac_value}")
        socketio.emit('display_update', {'value': ac_value})
    elif port == 2:
        print(f"OUT 2 (Printer): Queuing {ac_value}")
        printer_buffer.append(ac_value)
        socketio.emit('printer_job_ready', {'queue_size': len(printer_buffer)})
    elif port == 3:
        print(f"OUT 3 (Network): Broadcasting {ac_value} to LAN")
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = {'time': timestamp, 'source': 'CPU', 'payload': ac_value, 'status': 'Broadcast'}
        network_logs.append(log_entry)
        socketio.emit('network_broadcast_rx', {'payload': ac_value, 'log': log_entry})

def _do_step():
    """Core single-instruction step. Called by /step and auto_run_loop."""
    global cpu_state, main_memory, active_in_port, cpu_waiting_for_input

    if cpu_state["is_halted"] or cpu_waiting_for_input:
        return

    pc_val = cpu_state["PC"]
    ir_val = main_memory[pc_val]
    cpu_state["IR"] = ir_val
    opcode = (ir_val >> 4) & 0xF
    addr = ir_val & 0xF
    actual_addr = addr
    if addr == 0xF: actual_addr = main_memory[0xF] & 0xF

    ir_hex = disassemble(ir_val)

    if ir_hex not in cpu_state["THB"]:
        cpu_state["THB"][ir_hex] = {"count": 0, "cycles": 6}
    cpu_state["THB"][ir_hex]["count"] += 1
    count = cpu_state["THB"][ir_hex]["count"]

    is_mem_ref = opcode in [1, 2, 3, 4, 9, 0xA]
    cycle_cost = 6 if is_mem_ref else 4
    cpu_state["MPO_decision"] = False
    car_msb = "0"
    active_wires = ["wire-pc-data", "wire-ir-data", "wire-pc-addr"]
    active_comps = ["block-pc", "block-ir"]

    # FIX: Temporal bypass — track persistently and store triggering instruction
    if is_mem_ref and count >= 3:
        cpu_state["MPO_decision"] = True
        cpu_state["mode"] = "TEMPORAL BYPASS"
        cpu_state["temporal_mode_active"] = True
        cpu_state["temporal_mode_instruction"] = ir_hex
        cycle_cost = 4
        car_msb = "1"
        cpu_state["THB"][ir_hex]["cycles"] = 4
        active_wires.extend(["wire-mem-alu-bypass"])
        active_comps.extend(["block-mpo", "block-tbh"])
    else:
        cpu_state["mode"] = "NORMAL EXECUTION"

    next_pc = (pc_val + 1) & 0xF

    if opcode == 1:      # LDA
        cpu_state["AC"] = main_memory[actual_addr]
        active_wires.extend(["wire-alu-data", "wire-ac-data"])
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 2:    # STA — FIX: writes back to RAM
        main_memory[actual_addr] = cpu_state["AC"]
        active_wires.extend(["wire-ac-alu", "wire-alu-data"])
        active_comps.extend(["block-alu"])
    elif opcode == 3:    # ADD
        cpu_state["AC"] = (cpu_state["AC"] + main_memory[actual_addr]) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 4:    # SUB
        cpu_state["AC"] = (cpu_state["AC"] - main_memory[actual_addr]) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 9:    # AND
        cpu_state["AC"] = cpu_state["AC"] & main_memory[actual_addr]
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 0xA:  # OR
        cpu_state["AC"] = cpu_state["AC"] | main_memory[actual_addr]
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 5:    # MVI
        cpu_state["AC"] = actual_addr
        active_comps.extend(["block-ac"])
    elif opcode == 6:    # ADI
        cpu_state["AC"] = (cpu_state["AC"] + actual_addr) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 7:    # JMP
        next_pc = actual_addr
    elif opcode == 8:    # JNZ
        if cpu_state["AC"] != 0: next_pc = actual_addr
    elif opcode == 0xC:  # INC
        cpu_state["AC"] = (cpu_state["AC"] + 1) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif opcode == 0xD:  # DEC
        cpu_state["AC"] = (cpu_state["AC"] - 1) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif ir_val == 0xE0: # CLR
        cpu_state["AC"] = 0
        active_comps.extend(["block-ac"])
    elif ir_val == 0xE1: # SHL
        cpu_state["AC"] = (cpu_state["AC"] << 1) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif ir_val == 0xE2: # SHR
        cpu_state["AC"] = (cpu_state["AC"] >> 1) & 0xFF
        active_comps.extend(["block-alu", "block-ac"])
    elif ir_val == 0xF0: # HLT
        cpu_state["is_halted"] = True
        cpu_state["mode"] = "SYSTEM HALTED"
    elif ir_val == 0xE3: # INP — FIX: Halt and wait, do NOT advance PC
        cpu_waiting_for_input = True
        active_in_port = 1
        cpu_state["mode"] = "WAITING FOR INPUT"
        cpu_state["active_path"] = active_wires
        cpu_state["active_components"] = active_comps
        socketio.emit('request_input')
        broadcast_memory()
        socketio.emit('timm-tick', format_cpu_response())
        return  # Do not advance PC here
    elif ir_val == 0xE4: # OUT
        execute_out_instruction(1, cpu_state["AC"])
        active_comps.extend(["block-ac"])

    cpu_state["PC"] = next_pc
    cpu_state["cycles"] += cycle_cost
    cpu_state["clock_cycles_remaining"] = cycle_cost

    final_t_state = 3 if cpu_state["MPO_decision"] else 5
    cpu_state["CAR"] = f"{car_msb}{final_t_state:04b}"
    cpu_state["active_path"] = active_wires
    cpu_state["active_components"] = active_comps

    # FIX: Always broadcast memory after every step so RAM display stays current
    broadcast_memory()
    socketio.emit('timm-tick', format_cpu_response())

# ==========================================
# 6. HTTP ENDPOINTS
# ==========================================

@app.route('/')
def index():
    print("Serving index.html")
    return render_template('index.html')

@app.route('/step', methods=['POST'])
def step_instruction():
    global cpu_waiting_for_input
    if cpu_waiting_for_input:
        return jsonify(format_cpu_response())
    with auto_run_lock:
        _do_step()
    return jsonify(format_cpu_response())

@app.route('/run', methods=['GET'])
def start_auto_run():
    global keep_running, auto_run_thread
    # FIX: Prevent duplicate threads
    if not keep_running and (auto_run_thread is None or not auto_run_thread.is_alive()):
        keep_running = True
        auto_run_thread = threading.Thread(target=auto_run_loop, daemon=True)
        auto_run_thread.start()
    return jsonify({"status": "running"})

# FIX: /stop endpoint pauses auto-run without resetting CPU state
@app.route('/stop', methods=['GET'])
def stop_auto_run():
    global keep_running
    keep_running = False
    cpu_state["mode"] = "PAUSED"
    socketio.emit('timm-tick', format_cpu_response())
    return jsonify({"status": "stopped"})

@app.route('/reset', methods=['GET'])
def reset_cpu():
    global cpu_state, keep_running, cpu_waiting_for_input, active_in_port
    keep_running = False
    cpu_waiting_for_input = False
    active_in_port = 0
    cpu_state.update({
        "PC": 0, "AC": 0, "IR": 0, "CAR": "00000", "cycles": 0,
        "clock_cycles_remaining": 0,
        "mode": "IDLE", "MPO_decision": False, "THB": {},
        "is_halted": False, "fgi_flag": False, "input_buffer": 0,
        "active_path": [], "active_components": [],
        "temporal_mode_active": False, "temporal_mode_instruction": ""
    })
    socketio.emit('timm-tick', format_cpu_response())
    socketio.emit('display_update', {'value': 0})
    return jsonify(format_cpu_response())

# FIX: /clear_cpu clears registers only, leaves RAM intact
@app.route('/clear_cpu', methods=['GET'])
def clear_cpu():
    global cpu_state, keep_running, cpu_waiting_for_input, active_in_port
    keep_running = False
    cpu_waiting_for_input = False
    active_in_port = 0
    cpu_state.update({
        "PC": 0, "AC": 0, "IR": 0, "CAR": "00000", "cycles": 0,
        "clock_cycles_remaining": 0,
        "mode": "IDLE", "MPO_decision": False, "THB": {},
        "is_halted": False, "fgi_flag": False, "input_buffer": 0,
        "active_path": [], "active_components": [],
        "temporal_mode_active": False, "temporal_mode_instruction": ""
    })
    socketio.emit('timm-tick', format_cpu_response())
    return jsonify(format_cpu_response())

# ==========================================
# 7. SOCKET.IO ROUTES
# ==========================================

@socketio.on('connect')
def handle_connect():
    print("Frontend connected via WebSocket!")
    socketio.emit('timm-tick', format_cpu_response())
    broadcast_memory()

@socketio.on('set_speed')
def handle_speed_update(data):
    global clock_delay
    try:
        hz = float(data['speed'])
        if hz > 0: clock_delay = 1.0 / hz
    except: pass

@socketio.on('toggle_clock')
def handle_toggle(data):
    global clock_running
    clock_running = data.get('running', False)

def broadcast_memory():
    bin_mem = [f"{v:08b}" for v in main_memory]
    mnem_mem = [disassemble(v) for v in main_memory]
    hex_mem = [f"{v:02X}" for v in main_memory]
    socketio.emit('memory_sync', {
        'memory_bin': bin_mem,
        'memory_mnem': mnem_mem,
        'memory_hex': hex_mem   # FIX: include hex for RAM hex-mode display
    })

@socketio.on('request_memory_sync')
def handle_req_mem_sync():
    broadcast_memory()

@socketio.on('update_memory_slot')
def handle_update_mem(data):
    addr = int(data['address'])
    main_memory[addr] = assemble(data['value']) & 0xFF
    broadcast_memory()

@socketio.on('clear_memory')
def handle_clear_mem():
    global main_memory
    main_memory = [0] * 16
    broadcast_memory()

@socketio.on('hd_request_file_dialog')
def handle_file_dialog(data):
    global loaded_file_path
    try:
        root = tk.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        filepath = filedialog.askopenfilename(
            title="Select File",
            filetypes=[("TBT Assembly Files", "*.tbt"), ("All Files", "*.*")]
        )
        root.destroy()
        if filepath:
            loaded_file_path = filepath
            if basic_tbt_assembler(filepath):
                table_data = [
                    {'address': f"{addr:01X}", 'data': c['text'], 'hex': c['hex']}
                    for addr, c in sorted(hard_disk_buffer.items())
                ]
                socketio.emit('hd_file_loaded', {
                    'table': table_data,
                    'filename': os.path.basename(filepath)
                })
            else:
                socketio.emit('hd_error', {'msg': 'Failed to parse the file.'})
    except Exception as e:
        print(f"File Dialog Error: {e}")

@socketio.on('hd_save_to_ram')
def handle_save_to_ram():
    global hard_disk_buffer, main_memory
    if not hard_disk_buffer:
        socketio.emit('hd_error', {'msg': 'No file loaded. Please load a file first.'})
        return
    for addr, content in hard_disk_buffer.items():
        if 0 <= addr <= 15:
            main_memory[addr] = assemble(content['text']) & 0xFF
    hard_disk_buffer = {}
    socketio.emit('hd_cleared')
    broadcast_memory()

@socketio.on('hd_edit_file')
def handle_edit_file():
    global loaded_file_path
    if loaded_file_path and os.path.exists(loaded_file_path):
        subprocess.Popen(['notepad.exe', loaded_file_path])
    else:
        socketio.emit('hd_error', {'msg': 'No file loaded to edit.'})

# FIX: keypad_enter_pressed sets AC directly and advances PC past INP
@socketio.on('keypad_enter_pressed')
def handle_keypad_enter(data):
    global cpu_waiting_for_input, accumulator, cpu_state, active_in_port
    if cpu_waiting_for_input and active_in_port == 1:
        try:
            val = int(data['value']) & 0xFF
            accumulator = val
            cpu_state['AC'] = val
            cpu_state['input_buffer'] = val
            cpu_state['fgi_flag'] = True
            cpu_waiting_for_input = False
            active_in_port = 0
            cpu_state['PC'] = (cpu_state['PC'] + 1) & 0xF
            cpu_state['mode'] = 'NORMAL EXECUTION'
            cpu_state['cycles'] += 4
            cpu_state['clock_cycles_remaining'] = 4
            socketio.emit('input_accepted')
            broadcast_memory()
            socketio.emit('timm-tick', format_cpu_response())
        except Exception as e:
            print(f"Keypad error: {e}")

@socketio.on('keyboard_interrupt')
def handle_keyboard_interrupt(data):
    handle_keypad_enter(data)

@socketio.on('request_print_job')
def handle_print_request():
    global printer_buffer, printer_has_jobs
    if len(printer_buffer) > 0:
        socketio.emit('deliver_print_job', {'data': printer_buffer})
        printer_buffer = []
        printer_has_jobs = False
        socketio.emit('printer_queue_empty')

@socketio.on('pc_send_data')
def handle_pc_network_traffic(data):
    global cpu_waiting_for_input, active_in_port, accumulator, network_rx_buffer, network_logs, cpu_state
    sender_id = f"PC-{data['sender']}"
    try: payload = int(data['payload']) & 0xFF
    except: payload = 0

    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = {'time': timestamp, 'source': sender_id, 'payload': payload, 'status': 'Buffered'}
    network_logs.append(log_entry)
    network_rx_buffer.append(payload)
    socketio.emit('update_nc_buffer_ui', {
        'count': len(network_rx_buffer), 'last_data': payload, 'log': log_entry
    })

    if cpu_waiting_for_input and active_in_port == 2:
        val = network_rx_buffer.pop(0)
        accumulator = val
        cpu_state['AC'] = val
        cpu_waiting_for_input = False
        active_in_port = 0
        cpu_state['PC'] = (cpu_state['PC'] + 1) & 0xF
        cpu_state['mode'] = 'NORMAL EXECUTION'
        socketio.emit('input_accepted')
        socketio.emit('update_nc_buffer_ui', {'count': len(network_rx_buffer)})
        socketio.emit('timm-tick', format_cpu_response())

@socketio.on('request_network_logs')
def fetch_network_logs():
    socketio.emit('deliver_network_logs', {'logs': network_logs})

# ==========================================
# 8. START APP
# ==========================================

def start_socket_server():
    try:
        print("Starting Socket.IO Server...")
        socketio.run(app, host='127.0.0.1', port=5000, debug=False,
                     use_reloader=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        print(f"SERVER CRASH: {e}")

if __name__ == '__main__':
    try:
        server_thread = threading.Thread(target=start_socket_server, daemon=True)
        server_thread.start()
        time.sleep(1.5)
        print("Launching PyWebView Window...")
        window = webview.create_window(
            'TIMM: 4-Bit Operational Core Simulator',
            'http://127.0.0.1:5000',
            width=1631, height=913, resizable=False
        )
        webview.start()
    except Exception as e:
        print(f"WEBVIEW CRASH: {e}")
