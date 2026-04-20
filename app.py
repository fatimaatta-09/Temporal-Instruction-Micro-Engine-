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

# Manual bypass toggle — allows user to force-disable temporal mode for A/B comparison
temporal_bypass_manual_enabled = True  # True = hardware behaviour active; False = forced Normal mode

# CAR is 5-bit: MSB=0 → Normal (fetch from RAM via MBR, 6 cycles)
#               MSB=1 → Temporal Bypass (MBR held in high-Z, data direct RAM→ALU, 4 cycles)
# THB: 4×8 CAM — max 4 unique memory-reference signatures; flushes on JMP or Z-flag=1
cpu_state = {
    "PC": 0, "AC": 0, "IR": 0, "CAR": "00000", "cycles": 0,
    "clock_cycles_remaining": 0,
    "zero_flag": False,          # ALU Z-flag — set when ALU result == 0; drives JNZ; triggers THB flush
    "carry_flag": False,         # ALU C-flag — set on 8-bit overflow
    "neg_flag": False,           # ALU N-flag — set when MSB of result == 1
    "cycles_saved": 0,           # Cumulative clock cycles saved by Temporal Bypass (2 per bypassed instr)
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

    # Fixed single-operand instructions
    impl_map = {
        "NOP": 0x00, "HLT": 0xF0, "CLR": 0xE0, "SHL": 0xE1, "SHR": 0xE2,
        "ION": 0xE5, "IOF": 0xE6,
        # Bare INC/DEC (accumulator implied, operand nibble = 0)
        "INC": 0xC0, "DEC": 0xD0,
        # Port-1 shortcuts (backward compat)
        "INP": 0xE3, "OUT": 0xE4,
        # Port-specific OUT opcodes: OUT 1=0xE4, OUT 2=0xEB, OUT 3=0xEC
        "OUT 1": 0xE4, "OUT 2": 0xEB, "OUT 3": 0xEC,
        # Port-specific INP opcodes: INP 1=0xE3, INP 2=0xED
        "INP 1": 0xE3, "INP 2": 0xED,
    }
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
    impl_map = {
        0x00: "NOP", 0xF0: "HLT", 0xE0: "CLR", 0xE1: "SHL", 0xE2: "SHR",
        0xE3: "INP 1", 0xE4: "OUT 1", 0xE5: "ION", 0xE6: "IOF",
        0xEB: "OUT 2", 0xEC: "OUT 3",
        0xED: "INP 2",
    }
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
    global cpu_state, main_memory, temporal_bypass_manual_enabled
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
        "zero_flag": cpu_state["zero_flag"],
        "carry_flag": cpu_state["carry_flag"],
        "neg_flag": cpu_state["neg_flag"],
        "cycles_saved": cpu_state["cycles_saved"],
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
        "temporal_bypass_manual_enabled": temporal_bypass_manual_enabled,
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
    """
    Core single-instruction step implementing the TIME architecture spec:

    ALL instructions begin with the same 3-cycle fetch/decode (T0, T1, T2):
      T0: MAR ← PC
      T1: IR  ← M[MAR],  PC ← PC + 1
      T2: Decode + MPO queries THB

    Then diverge into Normal (6 cycles) or Temporal Bypass (4 cycles):

    Normal (MPO_match=0, CAR MSB=0):
      T3: MAR ← IR[3:0]
      T4: MBR ← M[MAR]          ← THE BOTTLENECK (eliminated by bypass)
      T5: AC  ← AC op MBR, SC←0

    Temporal Bypass (MPO_match=1, CAR MSB=1) — STA EXCLUDED:
      T3: AC  ← AC op M[operand]  (MBR held in high-Z, direct RAM→ALU)
      T4: SC ← 0  (bypassed, SC resets early)
      T5: (bypassed — CPU already on next fetch)

    THB eviction policy (hardware-triggered flush):
      • JMP decoded → CLR pulse to all THB D/T flip-flops → immediate full flush
      • Z-flag transitions to 1 → loop ended, flush THB

    THB capacity: maximum 4 unique memory-reference signatures (4×8 CAM).
    STA always uses Normal path (memory write requires MBR staging).
    """
    global cpu_state, main_memory, active_in_port, cpu_waiting_for_input
    global temporal_bypass_manual_enabled

    if cpu_state["is_halted"] or cpu_waiting_for_input:
        return

    pc_val  = cpu_state["PC"]
    ir_val  = main_memory[pc_val]
    cpu_state["IR"] = ir_val
    opcode  = (ir_val >> 4) & 0xF
    operand = ir_val & 0xF
    # Register-indirect: if operand nibble == 0xF, use M[0xF] as actual address
    actual_addr = (main_memory[0xF] & 0xF) if operand == 0xF else operand

    ir_sig = disassemble(ir_val)   # 8-bit IR signature (e.g. "ADD 3", "LDA F")

    # --- T2: Branch Decision + Pattern Recognition ---
    # Memory-Reference instructions (high-latency) that the THB selectively logs.
    # STA is memory-reference but its bypass is architecturally prohibited (write needs MBR).
    is_mem_ref_for_thb = opcode in [1, 2, 3, 4, 9, 0xA]   # LDA,STA,ADD,SUB,AND,OR
    is_bypass_eligible_op = opcode in [1, 3, 4, 9, 0xA]    # LDA,ADD,SUB,AND,OR (NOT STA)

    # THB: update only for memory-reference instructions; cap at 4 unique signatures
    if is_mem_ref_for_thb:
        if ir_sig not in cpu_state["THB"]:
            if len(cpu_state["THB"]) < 4:          # 4-entry CAM capacity limit
                cpu_state["THB"][ir_sig] = {"count": 0, "cycles": 6}
            # If THB is full and this is a new sig → no entry, no bypass for this sig
        if ir_sig in cpu_state["THB"]:
            cpu_state["THB"][ir_sig]["count"] += 1

    # --- MPO: Rule-of-Three threshold comparator ---
    thb_count = cpu_state["THB"].get(ir_sig, {}).get("count", 0)
    bypass_eligible = (
        is_bypass_eligible_op           # op supports bypass
        and thb_count >= 3              # frequency threshold met (≥3 hits)
        and temporal_bypass_manual_enabled  # user has not disabled bypass
    )

    # --- CAR MSB logic ---
    # MSB=0 → Normal path (MBR staging, 6 T-states total)
    # MSB=1 → Temporal Bypass (MBR high-Z, direct RAM→ALU, 4 T-states total)
    # Non-memory-reference instructions always cost 4 T-states (T0..T3, no MBR stage).
    car_msb    = "1" if bypass_eligible else "0"
    cycle_cost = 4 if bypass_eligible else (6 if is_mem_ref_for_thb else 4)

    cpu_state["MPO_decision"] = bypass_eligible

    if bypass_eligible:
        cpu_state["mode"] = "TEMPORAL BYPASS"
        cpu_state["temporal_mode_active"] = True
        cpu_state["temporal_mode_instruction"] = ir_sig
        cpu_state["THB"][ir_sig]["cycles"] = 4
        cpu_state["cycles_saved"] += 2   # 2 cycles saved per bypassed instruction (T4+T5 eliminated)
    else:
        # Only clear temporal_mode_active banner when we're NOT in a bypass AND it was active
        if cpu_state["temporal_mode_active"] and not bypass_eligible:
            cpu_state["mode"] = "NORMAL EXECUTION"
            # Don't clear temporal_mode_active yet — let THB flush events do it
        else:
            cpu_state["mode"] = "NORMAL EXECUTION"

    active_wires = ["wire-pc-data", "wire-ir-data", "wire-pc-addr"]
    active_comps = ["block-pc", "block-ir"]

    if bypass_eligible:
        active_wires.extend(["wire-mem-alu-bypass"])
        active_comps.extend(["block-mpo", "block-tbh"])

    next_pc = (pc_val + 1) & 0xF

    # ============================================================
    # EXECUTE PHASE — with ALU flag updates (Z, C, N)
    # ============================================================

    def set_flags(result_raw, result_8bit):
        """Update Z, C, N flags from a raw (pre-mask) ALU result."""
        cpu_state["zero_flag"]  = (result_8bit == 0)
        cpu_state["carry_flag"] = (result_raw > 0xFF) or (result_raw < 0)
        cpu_state["neg_flag"]   = bool(result_8bit & 0x80)

    if opcode == 1:      # LDA — load from RAM; bypass allowed
        result = main_memory[actual_addr]
        cpu_state["AC"] = result
        set_flags(result, result)
        active_wires.extend(["wire-alu-data", "wire-ac-data"])
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 2:    # STA — store AC to RAM; ALWAYS Normal path (no bypass)
        # STA requires MBR staging for write; the report explicitly excludes it from bypass
        main_memory[actual_addr] = cpu_state["AC"]
        # Override any bypass decision — STA cannot use bypass
        bypass_eligible = False
        car_msb = "0"
        cycle_cost = 6
        cpu_state["MPO_decision"] = False
        # STA is excluded from bypass by is_bypass_eligible_op (opcode 2 not in set),
        # so cycles_saved was never incremented for STA — no undo needed.
        active_wires.extend(["wire-ac-alu", "wire-alu-data"])
        active_comps.extend(["block-alu"])

    elif opcode == 3:    # ADD — bypass allowed
        raw = cpu_state["AC"] + main_memory[actual_addr]
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 4:    # SUB — bypass allowed
        raw = cpu_state["AC"] - main_memory[actual_addr]
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 9:    # AND — bypass allowed
        result = cpu_state["AC"] & main_memory[actual_addr]
        cpu_state["AC"] = result
        set_flags(result, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 0xA:  # OR — bypass allowed
        result = cpu_state["AC"] | main_memory[actual_addr]
        cpu_state["AC"] = result
        set_flags(result, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 5:    # MVI — immediate, single-cycle, not logged by THB
        cpu_state["AC"] = actual_addr
        set_flags(actual_addr, actual_addr)
        active_comps.extend(["block-ac"])

    elif opcode == 6:    # ADI — immediate, single-cycle
        raw = cpu_state["AC"] + actual_addr
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 7:    # JMP — unconditional jump
        # HARDWARE FLUSH: JMP decoded → CLR pulse → full THB eviction (zero-cycle)
        next_pc = actual_addr
        cpu_state["THB"] = {}
        cpu_state["temporal_mode_active"] = False
        cpu_state["temporal_mode_instruction"] = ""
        cpu_state["MPO_decision"] = False
        print(f"JMP decoded → THB flushed (eviction policy)")

    elif opcode == 8:    # JNZ — jump if NOT zero (reads ALU Z-flag)
        if not cpu_state["zero_flag"]:
            next_pc = actual_addr
        # Note: if Z=1 here, THB flush happens below after flag is set

    elif opcode == 0xC:  # INC — register-reference, not THB-logged
        raw = cpu_state["AC"] + 1
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 0xD:  # DEC — register-reference
        raw = cpu_state["AC"] - 1
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif opcode == 0xB:  # MOV — internal register copy (not THB-logged)
        active_comps.extend(["block-ac"])

    elif ir_val == 0xE0: # CLR
        cpu_state["AC"] = 0
        set_flags(0, 0)
        active_comps.extend(["block-ac"])

    elif ir_val == 0xE1: # SHL
        raw = cpu_state["AC"] << 1
        result = raw & 0xFF
        cpu_state["AC"] = result
        set_flags(raw, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif ir_val == 0xE2: # SHR
        result = cpu_state["AC"] >> 1
        cpu_state["AC"] = result
        set_flags(result, result)
        active_comps.extend(["block-alu", "block-ac"])

    elif ir_val == 0xF0: # HLT
        cpu_state["is_halted"] = True
        cpu_state["mode"] = "SYSTEM HALTED"

    elif ir_val == 0xE3: # INP 1 (Keypad) — wait for input
        cpu_waiting_for_input = True
        active_in_port = 1
        cpu_state["mode"] = "WAITING FOR INPUT (KEYPAD)"
        cpu_state["active_path"] = active_wires
        cpu_state["active_components"] = active_comps
        socketio.emit('request_input')
        broadcast_memory()
        socketio.emit('timm-tick', format_cpu_response())
        return

    elif ir_val == 0xED: # INP 2 (Network) — read from buffer or wait
        if len(network_rx_buffer) > 0:
            val = network_rx_buffer.pop(0) & 0xFF
            cpu_state["AC"] = val
            set_flags(val, val)
            socketio.emit('update_nc_buffer_ui', {'count': len(network_rx_buffer)})
            # Show the received value on the display immediately (same as keypad INP)
            socketio.emit('display_update', {'value': val})
            active_comps.extend(["block-ac"])
        else:
            cpu_waiting_for_input = True
            active_in_port = 2
            cpu_state["mode"] = "WAITING FOR INPUT (NETWORK)"
            cpu_state["active_path"] = active_wires
            cpu_state["active_components"] = active_comps
            socketio.emit('request_input')
            broadcast_memory()
            socketio.emit('timm-tick', format_cpu_response())
            return

    elif ir_val == 0xE4: # OUT 1 — Display
        execute_out_instruction(1, cpu_state["AC"])
        active_comps.extend(["block-ac"])

    elif ir_val == 0xEB: # OUT 2 — Printer
        execute_out_instruction(2, cpu_state["AC"])
        active_comps.extend(["block-ac"])

    elif ir_val == 0xEC: # OUT 3 — Network
        execute_out_instruction(3, cpu_state["AC"])
        active_comps.extend(["block-ac"])

    # ============================================================
    # POST-EXECUTE: Z-flag triggered THB flush (loop-end detection)
    # Per report §2.6.1: when Z-flag transitions to 1, THB is flushed
    # immediately — the loop has ended, optimization logs are cleared
    # for the next workload.
    # ============================================================
    if cpu_state["zero_flag"] and opcode != 7:  # JMP already flushed
        old_thb_size = len(cpu_state["THB"])
        if old_thb_size > 0:
            cpu_state["THB"] = {}
            cpu_state["temporal_mode_active"] = False
            cpu_state["temporal_mode_instruction"] = ""
            cpu_state["MPO_decision"] = False
            print(f"Z-flag=1 → THB flushed ({old_thb_size} entries cleared, loop ended)")

    # ============================================================
    # FINALIZE: advance PC, tally cycles, set CAR
    # ============================================================
    cpu_state["PC"] = next_pc
    cpu_state["cycles"] += cycle_cost
    cpu_state["clock_cycles_remaining"] = cycle_cost

    # CAR 5-bit: [MSB=mode][4-bit T-state]
    # Normal final T-state = 5 (T0…T5); Bypass final T-state = 3 (T0…T3)
    final_t_state = 3 if bypass_eligible else 5
    cpu_state["CAR"] = f"{car_msb}{final_t_state:04b}"
    cpu_state["active_path"] = active_wires
    cpu_state["active_components"] = active_comps

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
    global printer_buffer, printer_has_jobs, network_rx_buffer, network_logs
    global temporal_bypass_manual_enabled
    keep_running = False
    cpu_waiting_for_input = False
    active_in_port = 0
    # Clear all peripheral buffers on cold boot
    printer_buffer = []
    printer_has_jobs = False
    network_rx_buffer = []
    network_logs = []
    # temporal_bypass_manual_enabled is NOT reset — it's a user preference
    cpu_state.update({
        "PC": 0, "AC": 0, "IR": 0, "CAR": "00000", "cycles": 0,
        "clock_cycles_remaining": 0,
        "zero_flag": False, "carry_flag": False, "neg_flag": False,
        "cycles_saved": 0,
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
        "zero_flag": False, "carry_flag": False, "neg_flag": False,
        "cycles_saved": 0,
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

@socketio.on('set_temporal_bypass_manual')
def handle_set_temporal_bypass(data):
    global temporal_bypass_manual_enabled
    temporal_bypass_manual_enabled = bool(data.get('enabled', True))
    print(f"Temporal Bypass Manual Toggle → {'ENABLED' if temporal_bypass_manual_enabled else 'DISABLED'}")
    # Push updated state so UI reflects immediately
    socketio.emit('timm-tick', format_cpu_response())

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
    # Cold-load: zero out all 16 slots first so no ghost instructions remain
    main_memory = [0] * 16
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
        # Re-parse after a short delay so that when the user saves in Notepad
        # and then clicks "Save to RAM", the buffer reflects the edited version.
        # We do a best-effort re-parse immediately; the user should click
        # "Load" again or "Save" after editing for a guaranteed sync.
        # For a guaranteed sync we also listen for a 'hd_reload_after_edit' event below.
    else:
        socketio.emit('hd_error', {'msg': 'No file loaded to edit.'})

@socketio.on('hd_reload_after_edit')
def handle_reload_after_edit():
    """Called by the frontend after the user closes Notepad to re-parse the file."""
    global loaded_file_path, hard_disk_buffer
    if loaded_file_path and os.path.exists(loaded_file_path):
        if basic_tbt_assembler(loaded_file_path):
            table_data = [
                {'address': f"{addr:01X}", 'data': c['text'], 'hex': c['hex']}
                for addr, c in sorted(hard_disk_buffer.items())
            ]
            socketio.emit('hd_file_loaded', {
                'table': table_data,
                'filename': os.path.basename(loaded_file_path)
            })
        else:
            socketio.emit('hd_error', {'msg': 'Failed to re-parse the edited file.'})
    else:
        socketio.emit('hd_error', {'msg': 'No file path available for reload.'})

# FIX: keypad_enter_pressed — sets AC, advances PC past INP, shows value on display
@socketio.on('keypad_enter_pressed')
def handle_keypad_enter(data):
    global cpu_waiting_for_input, accumulator, cpu_state, active_in_port
    if not cpu_waiting_for_input:
        return
    try:
        val = int(data['value']) & 0xFF
        accumulator = val
        cpu_state['AC'] = val
        cpu_state['input_buffer'] = val
        cpu_state['fgi_flag'] = True
        # Update ALU flags for the loaded value
        cpu_state['zero_flag'] = (val == 0)
        cpu_state['neg_flag'] = bool(val & 0x80)
        cpu_state['carry_flag'] = False
        cpu_waiting_for_input = False
        active_in_port = 0
        cpu_state['PC'] = (cpu_state['PC'] + 1) & 0xF
        cpu_state['mode'] = 'NORMAL EXECUTION'
        cpu_state['clock_cycles_remaining'] = 4
        cpu_state['cycles'] += 4
        # Emit input_accepted WITH the value so screen.html can render immediately
        socketio.emit('input_accepted', {'value': val})
        # Also emit display_update so the 7-segment shows the entered value right away
        # (mirroring what OUT 1 would do — the user typed a number, they should see it)
        socketio.emit('display_update', {'value': val})
        broadcast_memory()
        socketio.emit('timm-tick', format_cpu_response())
        print(f"Keypad input accepted: {val} (0x{val:02X}) → AC, PC advanced to {cpu_state['PC']:X}")
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
        cpu_state['zero_flag'] = (val == 0)
        cpu_state['neg_flag'] = bool(val & 0x80)
        cpu_state['carry_flag'] = False
        cpu_waiting_for_input = False
        active_in_port = 0
        cpu_state['PC'] = (cpu_state['PC'] + 1) & 0xF
        cpu_state['mode'] = 'NORMAL EXECUTION'
        cpu_state['cycles'] += 4
        cpu_state['clock_cycles_remaining'] = 4
        socketio.emit('input_accepted', {'value': val})
        socketio.emit('display_update', {'value': val})
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
            width=1468, height=822, resizable=False
        )
        webview.start()
    except Exception as e:
        print(f"WEBVIEW CRASH: {e}")
