"""System status sampler: CPU, RAM, GPU (NVML), NIC throughput, temperatures, power, disks."""
import asyncio
import logging
import shutil
import time
from pathlib import Path

import psutil

log = logging.getLogger("sysmon")

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML = True
except Exception:  # no driver / no GPU: GPU section stays empty
    _NVML = False

RAPL = Path("/sys/class/powercap")


class SysMon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.data: dict = {}
        self._net_prev = None
        self._rapl_prev: dict[str, tuple[float, int]] = {}
        psutil.cpu_percent(percpu=True)

    def _gpus(self):
        out = []
        if not _NVML:
            return out
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            g = {"index": i, "name": pynvml.nvmlDeviceGetName(h)}
            for key, fn in (
                ("util", lambda: pynvml.nvmlDeviceGetUtilizationRates(h).gpu),
                ("mem_util", lambda: pynvml.nvmlDeviceGetUtilizationRates(h).memory),
                ("enc", lambda: pynvml.nvmlDeviceGetEncoderUtilization(h)[0]),
                ("dec", lambda: pynvml.nvmlDeviceGetDecoderUtilization(h)[0]),
                ("temp", lambda: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)),
                ("power", lambda: pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0),
                ("power_limit", lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0),
                ("fan", lambda: pynvml.nvmlDeviceGetFanSpeed(h)),
                ("enc_sessions", lambda: pynvml.nvmlDeviceGetEncoderSessions(h).__len__()),
            ):
                try:
                    g[key] = fn()
                except Exception:
                    g[key] = None
            try:
                m = pynvml.nvmlDeviceGetMemoryInfo(h)
                g["mem_used"], g["mem_total"] = m.used, m.total
            except Exception:
                pass
            out.append(g)
        return out

    def _net(self, now):
        cur = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        out = []
        prev = self._net_prev
        self._net_prev = (now, cur)
        if not prev:
            return out
        dt = max(1e-3, now - prev[0])
        for nic, c in cur.items():
            if nic == "lo" or nic not in prev[1]:
                continue
            st = stats.get(nic)
            if st and not st.isup:
                continue
            p = prev[1][nic]
            rx = (c.bytes_recv - p.bytes_recv) * 8 / dt
            tx = (c.bytes_sent - p.bytes_sent) * 8 / dt
            speed = (st.speed if st else 0) or 0   # Mbit/s, 0 = unknown
            util = max(rx, tx) / (speed * 1e6) * 100 if speed else None
            out.append({"name": nic, "rx": rx, "tx": tx, "speed": speed, "util": util})
        return out

    def _temps(self):
        out = []
        try:
            for chip, items in psutil.sensors_temperatures().items():
                for s in items:
                    if chip == "coretemp" and not s.label.startswith("Package"):
                        continue
                    out.append({"name": f"{chip} {s.label}".strip(), "value": s.current,
                                "high": s.high, "critical": s.critical})
        except Exception:
            pass
        return out

    def _cpu_power(self, now):
        """RAPL package energy -> watts (needs read permission on energy_uj)."""
        out = []
        for d in sorted(RAPL.glob("intel-rapl:[0-9]")):
            try:
                e = int((d / "energy_uj").read_text())
                name = (d / "name").read_text().strip()
            except (OSError, ValueError):
                continue
            prev = self._rapl_prev.get(d.name)
            self._rapl_prev[d.name] = (now, e)
            if prev and e >= prev[1]:
                out.append({"name": name, "watts": (e - prev[1]) / 1e6 / (now - prev[0])})
        return out

    def _disks(self):
        out = []
        paths = self.cfg.section("paths")
        for key in ("record", "subclip"):
            try:
                u = shutil.disk_usage(paths[key])
                out.append({"name": key, "path": paths[key], "total": u.total, "used": u.used, "free": u.free})
            except OSError as e:
                out.append({"name": key, "path": paths[key], "error": str(e)})
        return out

    def sample(self):
        now = time.monotonic()
        vm = psutil.virtual_memory()
        per = psutil.cpu_percent(percpu=True)
        self.data = {
            "time": time.time(),
            "cpu": {"total": sum(per) / len(per) if per else 0, "per": per,
                    "load": list(psutil.getloadavg()), "count": psutil.cpu_count()},
            "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
            "gpu": self._gpus(),
            "net": self._net(now),
            "temps": self._temps(),
            "power": self._cpu_power(now),
            "disks": self._disks(),
        }

    async def run(self, period: float = 1.0):
        while True:
            try:
                await asyncio.to_thread(self.sample)
            except Exception:
                log.exception("sample")
            await asyncio.sleep(period)
