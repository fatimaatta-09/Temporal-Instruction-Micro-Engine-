# TIMM: Temporal Insight Maurice Machine

> **An Adaptive, Self-Optimizing Microprogrammed Processor Architecture**

![Version](https://img.shields.io/badge/version-1.0-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![Architecture](https://img.shields.io/badge/Architecture-8--bit-orange.svg)


## 1. Project Overview
The Temporal Insight Maurice Machine (TIMM) is a novel microprogrammed processor architecture and accompanying software simulator. Unlike traditional static processors that execute instructions rigidly, TIMM features an **Adaptive Execution Engine**. 

TIMM monitors its own execution history, logs instruction latency, and dynamically rewrites its micro-operations on the fly to bypass redundant data movements—a hardware-level process known as **Macro-Fusion**.

## 2. Problem Statement
Standard microprogrammed processors (such as the Mano Machine or the 1951 Maurice Wilkes architecture) suffer from significant execution latency because they fetch and execute repetitive sequences without temporal context. They operate as "static" machines: if a program runs a specific loop 1,000 times, the processor blindly fetches and executes the exact same slow micro-routine 1,000 times. 

This creates execution bottlenecks, specifically by wasting clock cycles on redundant memory buffer transfers (via the MBR) during predictable, sequential operations.

## 3. Architectural Innovation (The Intelligence Zone)
To resolve the latency issues of static execution, TIMM introduces a custom hardware logic layer physically inserted between the Instruction Register (IR) and the Control Unit. 

This "Intelligence Zone" consists of three integrated components:

### A. Temporal History Buffer (THB)
A metadata cache simulated as Content-Addressable Memory (CAM). It logs the opcode and the exact number of clock cycles utilized by recent instructions.
* **Hardware implementation:** A 16-row SRAM block queried by the opcode, combined with a 4-bit hardware Up-Counter that tracks T-states.
* **Function:** Identifies operations that historically consume high clock cycles and sets a hardware `THB_Slow_Flag`.

### B. Microprogram Optimizer (MPO)
The decision-making logic matrix. It acts as a multiplexer (MUX) selector intercepting the Control Address Register (CAR).
* **Hardware implementation:** A Programmable Logic Array (PLA) monitoring the THB and the Instruction Prefetch Buffer.
* **Logic Trigger:** The physical optimization is triggered by the following combinational logic:
  $MPO_{trigger} = (IR == \text{LOAD}) \land (\text{Prefetch} == \text{ADD}) \land (THB_{Slow} == 1)$
* **Function:** When triggered, the MUX disconnects the standard micro-ROM address and forces the Control Unit to jump to a highly optimized, fused micro-routine.

### C. Adaptive Cycle Predictor (ACP)
A performance monitor that tracks overall elapsed time and calculates the total clock cycles saved by the architecture's predictive routing.

## 4. Hardware Architecture & Datapath
TIMM is designed around a single 8-bit common data bus utilizing Tri-State Buffers, augmented by a secondary **Temporal Bypass Bus** utilized exclusively during optimized execution.

* **System Architecture:** 8-bit Data / 8-bit Instructions
* **Main Memory (RAM):** 16 words × 8 bits
* **Control Memory (ROM):** 32 Micro-instructions
* **General Purpose Registers:** * `R0` (Accumulator)
  * `R1`, `R2`, `R3` (Scratchpad Registers)
* **Special Purpose Registers:**
  * `PC` (Program Counter - 4 bits)
  * `MAR` (Memory Address Register - 4 bits)
  * `MBR` (Memory Buffer Register - 8 bits)
  * `IR` (Instruction Register - 8 bits)
  * `CAR` (Control Address Register - 5 bits)
  * `FLAGS` (Zero and Carry Flags - 2 bits)

## 5. Instruction Set Architecture (ISA)
TIMM utilizes a single 8-bit instruction format: `[Opcode: 4 bits (MSB)] | [Operand: 4 bits (LSB)]`.

**Opcode Mapping (16 Instructions):**
* `0000`: `ADD` (R0 <- R0 + Memory)
* `0001`: `SUB` (R0 <- R0 - Memory)
* `0010`: `AND` (R0 <- R0 AND Memory)
* `0011`: `OR`  (R0 <- R0 OR Memory)
* `0100`: `XOR` (R0 <- R0 XOR Memory)
* `0101`: `NOT` (Invert R0)
* `0110`: `SHL` (Shift Left R0)
* `0111`: `SHR` (Shift Right R0)
* `1000`: `LOAD` (Load Memory into R0)
* `1001`: `STORE` (Store R0 into Memory)
* `1010`: `LOADI` (Load Immediate Value)
* `1011`: `JMP` (Jump Unconditionally)
* `1100`: `JZ`  (Jump if Zero Flag is Set)
* `1101`: `IN`  (Program-controlled I/O Read)
* `1110`: `OUT` (Program-controlled I/O Write)
* `1111`: `HLT` (Halt System)

## 6. Execution Modes & Micro-Operations
The processor shifts states dynamically based on the MPO.

### Normal Mode (Standard Sequence)
Executes instructions standardly over 6 clock cycles (T-states):
* `T0:` MAR <- PC
* `T1:` MBR <- Memory, PC <- PC + 1
* `T2:` IR <- MBR 
* `T3:` MAR <- IR[Operand]
* `T4:` MBR <- Memory
* `T5:` R0 <- R0 + MBR 

### Temporal Optimized Mode (Macro-Fusion Sequence)
Activated dynamically when the MPO detects a predictable bottleneck (e.g., `LOAD` followed by `ADD`). Executes the equivalent of two instructions in 4 clock cycles:
* `T0:` MAR <- PC, PC <- PC + 1 
* `T1:` IR <- Memory, MAR <- Memory[Operand] *(MBR is bypassed)*
* `T2:` R0 <- R0 + Memory *(Temporal Bypass Bus routes directly to ALU)*
* `T3:` Settle Phase / Flag Update

## 7. The Simulator Application
The included application is a standalone graphical simulator designed to visualize the internal block-level state of the TIMM architecture. It features a retro-styled, highly educational interface allowing users to manually load instructions, step through micro-operations, and observe the datapath bus logic in real-time.

## 8. Project Team
* **Ali Kamran** 
* **Fatima Rehman** 
