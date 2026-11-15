"""LabVIEW functional test 1: nominal sequence, four abort cases, blocked start, log file.

Requires LabVIEW 2026 running with Rocket_DAQ_Main.vi open and idle, and pywin32.
The VI is driven through LabVIEW's ActiveX server and stopped with its own STOP control.
"""
import win32com.client, time, sys, os
lv = win32com.client.dynamic.Dispatch("LabVIEW.Application")
p = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Rocket_DAQ_Main.vi"))
vi = lv.GetVIReference(p, "", False, 0)
for m in ("Run","GetControlValue","SetControlValue"): vi._FlagAsMethod(m)
print("exec state:", vi.ExecState)
IN = {"cmd":"Command","estop":"E-Stop 0/1","fault":"Fault inject","total":"Target flow kg/s","mr":"Target MR",
      "plimit":"Abort limit bar","hold":"Hold time s","fftch":"FFT channel"}
OUT = {"state":"State","tsec":"Time s","totalflow":"Total flow kg/s","mixratio":"Mixture ratio","thrust":"Thrust N",
       "cmdox":"Ox valve V","cmdfuel":"Fuel valve V","abortcode":"Abort code","aborttime":"Abort time s"}
def setc(**kw):
    for k,v in kw.items(): vi.SetControlValue(IN[k], float(v))
def get(n): return vi.GetControlValue(OUT[n])
def snap(): return " ".join(f"{n}={get(n):.3f}" for n in OUT)
NAMES = ["IDLE","PRE-CHECK","READY","START","RAMP","HOLD","SHUTDOWN","COMPLETE","ABORT"]
def wait_state(s, timeout):
    end = time.time()+timeout; last=None
    while time.time()<end:
        st = int(get("state"))
        if st != last: print(f"   t={get('tsec'):6.1f}  -> {NAMES[st]}"); last = st
        if st == s: return True
        time.sleep(0.05)
    return False
def reset():
    setc(cmd=0); time.sleep(0.3); setc(cmd=4); r = wait_state(0, 5); setc(cmd=0); time.sleep(0.2); return r
vi.SetControlValue("stop", False)
setc(cmd=0, estop=0, fault=0, total=2.5, mr=2.0, plimit=28.0, hold=6.0, fftch=0)
vi.Run(True); time.sleep(1.0)
print("RUN 1 nominal")
setc(cmd=1); ok = wait_state(2, 15); setc(cmd=2)
ok &= wait_state(5, 15); time.sleep(2.5); print("   HOLD:", snap())
spec = vi.GetControlValue("FFT amplitude spectrum (1 bin = 10 Hz)")
k = max(range(1, len(spec)), key=lambda i: spec[i]); print(f"   FFT: {len(spec)} bins, dominant {k*10} Hz, amplitude {spec[k]*2**0.5:.3f} (peak)")
eng = vi.GetControlValue("Slow ch scaled [50]"); raw = vi.GetControlValue("Slow ch raw V [50]")
print("   PT-01 tank %.2f bar (raw %.3f V), PT-07 chamber %.2f bar (raw %.3f V), TT-01 %.1f degC, FT-01 %.3f kg/s" % (eng[0], raw[0], eng[6], raw[6], eng[20], eng[40]))
ok &= wait_state(6, 10); ok &= wait_state(7, 10); ok &= reset()
print("RUN 2 overpressure abort"); setc(cmd=1); ok &= wait_state(2, 15); setc(cmd=2); ok &= wait_state(5, 15); time.sleep(1.0)
setc(fault=1); ok &= wait_state(8, 3); print("   ABORT:", snap())
setc(cmd=0); time.sleep(0.3); setc(cmd=4); time.sleep(0.5); print("   reset while fault active ->", NAMES[int(get('state'))]); ok &= int(get('state')) == 8
setc(fault=0); ok &= reset()
print("RUN 3 sensor disconnect"); setc(cmd=1); ok &= wait_state(2, 15); setc(cmd=2); ok &= wait_state(4, 15); time.sleep(1.0)
setc(fault=2); ok &= wait_state(8, 3); print("   ABORT:", snap()); setc(fault=0); ok &= reset()
print("RUN 4 stuck sensor"); setc(cmd=1); ok &= wait_state(2, 15); setc(cmd=2); ok &= wait_state(5, 15)
setc(fault=3); ok &= wait_state(8, 3); print("   ABORT:", snap()); setc(fault=0); ok &= reset()
print("RUN 5 e-stop"); setc(cmd=1); ok &= wait_state(2, 15); setc(estop=1); ok &= wait_state(8, 3); print("   ABORT:", snap()); setc(estop=0); ok &= reset()
print("RUN 6 fault before start: pre-check must not pass"); setc(fault=2); time.sleep(0.3); setc(cmd=1); r = wait_state(2, 12)
print("   reached READY:", r, "-> state", NAMES[int(get('state'))]); ok &= (not r); setc(fault=0, cmd=0)
vi.SetControlValue("stop", True); time.sleep(1.0)
print("stopped; exec state:", vi.ExecState)
f = r"C:\Users\Public\Documents\rocket_daq_slow_log.csv"
rows = open(f).read().splitlines()
print("CSV rows:", len(rows), "cols:", len(rows[0].split("\t")), "size MB:", round(os.path.getsize(f)/1e6,1))
print("ALL OK" if ok else "SOMETHING FAILED")
