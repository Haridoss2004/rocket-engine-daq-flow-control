"""LabVIEW functional test 2: set-point changes, saturation, FFT channels, stop, manual abort, digital outputs.

Requires LabVIEW 2026 running with Rocket_DAQ_Main.vi open and idle, and pywin32.
The VI is driven through LabVIEW's ActiveX server and stopped with its own STOP control.
"""
import os
import win32com.client, time, sys
lv = win32com.client.dynamic.Dispatch("LabVIEW.Application")
p = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Rocket_DAQ_Main.vi"))
vi = lv.GetVIReference(p, "", False, 0)
for m in ("Run","GetControlValue","SetControlValue"): vi._FlagAsMethod(m)
print("exec state:", vi.ExecState)
IN = {"cmd":"Command","estop":"E-Stop 0/1","fault":"Fault inject","total":"Target flow kg/s","mr":"Target MR",
      "plimit":"Abort limit bar","hold":"Hold time s","fftch":"FFT channel"}
def setc(**kw):
    for k,v in kw.items(): vi.SetControlValue(IN[k], float(v))
def get(n): return vi.GetControlValue(n)
NAMES = ["IDLE","PRE-CHECK","READY","START","RAMP","HOLD","SHUTDOWN","COMPLETE","ABORT"]
def st(): return int(get("State"))
def wait_state(s, timeout):
    end = time.time()+timeout
    while time.time()<end:
        if st() == s: return True
        time.sleep(0.05)
    print("   !! timeout waiting for", NAMES[s], "state is", NAMES[st()]); return False
def cmd(n): setc(cmd=0); time.sleep(0.25); setc(cmd=n); time.sleep(0.25)
def peak():
    sp = get("FFT amplitude spectrum (1 bin = 10 Hz)"); k = max(range(1, len(sp)), key=lambda i: sp[i]); return k*10, sp[k]*2**0.5
vi.SetControlValue("stop", False)
setc(cmd=0, estop=0, fault=0, total=2.5, mr=2.0, plimit=28.0, hold=14.0, fftch=0)
vi.Run(True); time.sleep(1.0)
if st() != 0: cmd(4); wait_state(0, 5)
ok = True
print("A set-point change during HOLD")
cmd(1); ok &= wait_state(2, 15); cmd(2); ok &= wait_state(5, 15); time.sleep(1.5)
setc(total=1.8, mr=2.4); time.sleep(2.5)
tf, mr = get("Total flow kg/s"), get("Mixture ratio"); print(f"   targets 1.8 / 2.4 -> measured {tf:.3f} / {mr:.3f}")
ok &= abs(tf-1.8) < 0.04 and abs(mr-2.4) < 0.05
print("B unreachable target saturates at 10 V, stays in HOLD")
setc(total=9.0, mr=2.0); time.sleep(2.5)
vo, vf = get("Ox valve V"), get("Fuel valve V"); print(f"   valves {vo:.2f} / {vf:.2f} V, state {NAMES[st()]}")
ok &= max(vo, vf) >= 9.99 and max(vo, vf) <= 10.0 and st() == 5
setc(total=2.5); time.sleep(2.0)
print(f"   back to 2.5 -> {get('Total flow kg/s'):.3f} kg/s"); ok &= abs(get("Total flow kg/s")-2.5) < 0.05
print("C FFT channel selection and instability tone")
setc(fftch=24); time.sleep(0.6); f, a = peak(); print(f"   channel 24 reference: {f} Hz, {a:.3f}"); ok &= f == 1000 and abs(a-1.0) < 0.05
setc(fftch=12); time.sleep(0.6); f, a = peak(); print(f"   channel 12: {f} Hz, {a:.3f} (expected 840 Hz)"); ok &= f == 840
setc(fftch=0, fault=7); time.sleep(0.6); f, a = peak(); print(f"   instability tone on: {f} Hz, {a:.3f} (expected 1850 Hz, 3.0)"); ok &= f == 1850 and abs(a-3.0) < 0.1
ok &= st() == 5; setc(fault=0)
print("D operator stop (command 3) during HOLD -> controlled shutdown")
cmd(3); ok &= wait_state(6, 2); ok &= wait_state(7, 6); print("   reached", NAMES[st()], "valves", round(get("Ox valve V"),3), round(get("Fuel valve V"),3))
cmd(4); ok &= wait_state(0, 3)
print("E manual abort (command 5) during RAMP")
cmd(1); ok &= wait_state(2, 15); cmd(2); ok &= wait_state(4, 15); time.sleep(0.8)
cmd(5); ok &= wait_state(8, 2); print("   abort code", get("Abort code")); ok &= get("Abort code") == 6
cmd(4); ok &= wait_state(0, 3)
print("F cancel from READY (command 3) returns to IDLE")
cmd(1); ok &= wait_state(2, 15); cmd(3); ok &= wait_state(0, 2)
print("G lowered abort limit trips in HOLD")
setc(hold=6.0); cmd(1); ok &= wait_state(2, 15); cmd(2); ok &= wait_state(5, 15); time.sleep(1.0)
setc(plimit=15.0); ok &= wait_state(8, 2); print("   abort code", get("Abort code")); ok &= get("Abort code") == 1
setc(plimit=28.0); cmd(4); ok &= wait_state(0, 3)
print("H digital outputs in ABORT vs HOLD checked via DIO array")
cmd(1); ok &= wait_state(2, 15); cmd(2); ok &= wait_state(5, 15); time.sleep(0.5)
d = get("DIO [60]"); print("   HOLD: main valves", d[30], d[31], "vents", d[32], "igniter", d[37], "| DI main open", d[6], "flame", d[21])
ok &= d[30] == 1 and d[32] == 0 and d[37] == 0 and d[6] == 1 and d[21] == 1
setc(estop=1); ok &= wait_state(8, 2); time.sleep(0.3); d = get("DIO [60]")
print("   ABORT: main valves", d[30], d[31], "vents", d[32], "purge", d[34], "abort lamp", d[43], "| DI e-stop healthy", d[0])
ok &= d[30] == 0 and d[32] == 1 and d[34] == 1 and d[43] == 1 and d[0] == 0
setc(estop=0); cmd(4); ok &= wait_state(0, 3); setc(cmd=0)
print("I overpressure on a NON-chamber pressure channel (tank PT-01, fixed 50 bar limit) also aborts")
cmd(1); ok &= wait_state(2, 15); cmd(2); ok &= wait_state(5, 15); time.sleep(0.8)
print("   before fault: state", NAMES[st()], "| PT-01 %.1f bar, PT-07 %.1f bar" % (get("Slow ch scaled [50]")[0], get("Slow ch scaled [50]")[6]))
setc(fault=4); ok &= wait_state(8, 2); time.sleep(0.3)
print("   abort code", get("Abort code"), "|", get("Abort reason")); ok &= get("Abort code") == 1
cmd(4); time.sleep(0.4); print("   reset while fault active ->", NAMES[st()]); ok &= st() == 8
setc(fault=0); cmd(4); ok &= wait_state(0, 3); setc(cmd=0)
vi.SetControlValue("stop", True); time.sleep(1.0)
print("stopped; exec state:", vi.ExecState)
print("ALL OK" if ok else "SOMETHING FAILED")
