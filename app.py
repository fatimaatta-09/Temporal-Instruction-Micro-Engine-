import os
import sys
import webview
from flask import Flask, jsonify, render_template

# --- 1. PYINSTALLER PATH RESOLVER ---
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# Initialize Flask with the safe paths
app = Flask(__name__, 
            template_folder=resource_path('templates'),
            static_folder=resource_path('static'))

# --- Helper Function for Hardware Formatting ---
def to_bin(value, bits):
    return format(value & ((1 << bits) - 1), f'0{bits}b')

# --- THE CPU BRAIN ---
class TIMM_CPU:
    def __init__(self):
        self.memory = [0] * 16
        self.PC = 0
        self.AC = 0
        self.IR = 0
        self.MAR = 0
        self.MBR = 0
        self.THB = {}
        self.previous_opcode = ""
        self.MPO_decision = False
        self.mode = "NORMAL"
        self.total_cycles = 0
        self.cycles_saved = 0
        self.active_wires = []
        self.active_chips = []

    def reset(self):
        self.__init__()

    def step(self):
        self.total_cycles += 1
        self.PC = (self.PC + 1) & 0b1111 
        self.active_wires = ["wire-pc-data", "wire-pc-addr"]
        self.active_chips = ["block-pc"]
        self.mode = "FETCHING"

    def get_frontend_state(self):
        return {
            "PC": to_bin(self.PC, 4),
            "AC": to_bin(self.AC, 8),
            "IR": to_bin(self.IR, 8),
            "current_instruction": "WAITING", 
            "next_instruction": "WAITING",
            "mode": self.mode,
            "active_path": self.active_wires,
            "active_components": self.active_chips,
            "THB": self.THB,
            "MPO_decision": self.MPO_decision,
            "cycles": self.total_cycles
        }

timm_machine = TIMM_CPU()

# --- FLASK WEB ROUTES ---
@app.route('/')
def home():
    return render_template('index.html') 

@app.route('/step', methods=['POST'])
def step_clock():
    timm_machine.step()
    return jsonify(timm_machine.get_frontend_state())

@app.route('/reset', methods=['GET'])
def reset_core():
    timm_machine.reset()
    return jsonify(timm_machine.get_frontend_state())

if __name__ == '__main__':
    # THIS IS THE ELECTRON MAGIC:
    # Creates a native desktop window and feeds the Flask app directly into it!
    window = webview.create_window('TIMM: 4-Bit Operational Core Simulator', app, width=1750, height=980, resizable=False)
    webview.start()