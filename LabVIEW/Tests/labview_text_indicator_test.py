"""LabVIEW functional test 3: state-name and abort-reason text indicators, dominant frequency.

Requires LabVIEW 2026 running with Rocket_DAQ_Main.vi open and idle, and pywin32.
"""
import os
import win32com.client, time
lv = win32com.client.dynamic.Dispatch("LabVIEW.Application")
p = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Rocket_DAQ_Main.vi"))
vi = lv.GetVIReference(p, "", False, 0)
for m in ("Run","GetControlValue","SetControlValue"): vi._FlagAsMethod(m)
print("exec state:", vi.ExecState)
def setv(n, v): vi.SetControlValue(n, float(v))
def get(n): return vi.GetControlValue(n)
def st(): return int(get("State"))
def wait(s, t):
    end = time.time() + t
    while time.time() < end:
        if st() == s: return True
        time.sleep(0.05)
    return False
def cmd(n): setv("Command", 0); time.sleep(0.25); setv("Command", n); time.sleep(0.25)
vi.SetControlValue("stop", False)
for n, v in (("Command",0),("E-Stop 0/1",0),("Fault inject",0),("Target flow kg/s",2.5),("Target MR",2.0),("Abort limit bar",28),("Hold time s",10),("FFT channel",0)): setv(n, v)
vi.Run(True); time.sleep(1.0)
if st() != 0: cmd(4); wait(0, 5)
ok = True
names = []
print("IDLE     ->", repr(get("State name")), "| abort reason:", repr(get("Abort reason"))); ok &= get("State name") == "IDLE" and get("Abort reason") == "none"
cmd(1); time.sleep(0.5); names.append(get("State name")); ok &= wait(2, 15); names.append(get("State name"))
cmd(2); time.sleep(1.0); names.append(get("State name")); ok &= wait(4, 15); time.sleep(0.3); names.append(get("State name"))
ok &= wait(5, 15); time.sleep(3.0); names.append(get("State name"))
print("state names seen:", names); ok &= names == ["PRE-CHECK", "READY", "START (simulated)", "RAMP", "HOLD"]
df = get("Dominant freq Hz"); print("dominant frequency:", df, "Hz"); ok &= df == 1200
setv("Fault inject", 1); ok &= wait(8, 3); time.sleep(1.2)
print("ABORT    ->", repr(get("State name")), "|", repr(get("Abort reason"))); ok &= get("State name") == "ABORT" and get("Abort reason").startswith("OVERPRESSURE")
setv("Fault inject", 0); cmd(4); ok &= wait(0, 5)
for code, fault, text in ((2, 2, "SENSOR FAILURE - pressure sensor out of range"), (3, 3, "SENSOR FAILURE - stale")):
    cmd(1); ok &= wait(2, 15); cmd(2); ok &= wait(5, 15); setv("Fault inject", fault); ok &= wait(8, 3); time.sleep(0.3)
    r = get("Abort reason"); print(f"code {code}   ->", repr(r)); ok &= r.startswith(text)
    setv("Fault inject", 0); cmd(4); ok &= wait(0, 5)
cmd(1); ok &= wait(2, 15); setv("E-Stop 0/1", 1); ok &= wait(8, 3); time.sleep(0.3); r = get("Abort reason"); print("e-stop   ->", repr(r)); ok &= r == "EMERGENCY STOP"
setv("E-Stop 0/1", 0); cmd(4); ok &= wait(0, 5)
cmd(1); ok &= wait(2, 15); cmd(5); ok &= wait(8, 3); time.sleep(0.3); r = get("Abort reason"); print("manual   ->", repr(r)); ok &= r == "MANUAL ABORT"
cmd(4); ok &= wait(0, 5); setv("Command", 0)
vi.SetControlValue("stop", True); time.sleep(1.0)
print("stopped; exec state:", vi.ExecState)
print("ALL OK" if ok else "SOMETHING FAILED")
