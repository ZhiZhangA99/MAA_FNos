import sys
sys.path.insert(0, "/opt/maa/Python")
from asst.asst import Asst

print("Asst imported OK")
Asst.load("/opt/maa")
print("MaaCore loaded OK")
try:
    print("Version:", Asst.get_version())
except Exception as e:
    print("get_version err:", e)
