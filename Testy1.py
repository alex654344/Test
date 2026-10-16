

import argparse
import binascii
import ctypes
import ctypes.util
import os
import struct
import sys
import time
import threading
import queue
from multiprocessing import cpu_count

import requests

RANDOMX_HASH_SIZE = 32
RANDOMX_FLAG_DEFAULT     = 0
RANDOMX_FLAG_LARGE_PAGES = 1
RANDOMX_FLAG_HARD_AES    = 2
RANDOMX_FLAG_FULL_MEM    = 4
RANDOMX_FLAG_JIT         = 8
RANDOMX_FLAG_SECURE      = 16


# ---------- low-level RandomX (shared cache/dataset, per-thread VM) ----------

class RandomXEngine:
    """One process-wide cache/dataset + one VM per mining thread."""

    def __init__(self, lib_path=None, full_mem=True, large_pages=False, jit=True):
        if lib_path is None:
            lib_path = ctypes.util.find_library("randomx") or "librandomx.so"
        self.lib = ctypes.CDLL(lib_path)

        L = self.lib
        L.randomx_get_flags.restype = ctypes.c_uint
        L.randomx_alloc_cache.argtypes = [ctypes.c_uint]
        L.randomx_alloc_cache.restype = ctypes.c_void_p
        L.randomx_init_cache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.randomx_release_cache.argtypes = [ctypes.c_void_p]
        L.randomx_alloc_dataset.argtypes = [ctypes.c_uint]
        L.randomx_alloc_dataset.restype = ctypes.c_void_p
        L.randomx_dataset_item_count.restype = ctypes.c_ulong
        L.randomx_init_dataset.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        L.randomx_release_dataset.argtypes = [ctypes.c_void_p]
        L.randomx_create_vm.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        L.randomx_create_vm.restype = ctypes.c_void_p
        L.randomx_vm_set_cache.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.randomx_vm_set_dataset.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.randomx_destroy_vm.argtypes = [ctypes.c_void_p]
        L.randomx_calculate_hash.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p
        ]

        flags = L.randomx_get_flags()
        if jit:
            flags |= RANDOMX_FLAG_JIT
        if large_pages:
            flags |= RANDOMX_FLAG_LARGE_PAGES
        if full_mem:
            flags |= RANDOMX_FLAG_FULL_MEM

        self.flags = flags
        self.full_mem = full_mem
        self.current_seed = None
        self.lock = threading.Lock()

        self.cache = L.randomx_alloc_cache(flags)
        if not self.cache:
            raise RuntimeError("randomx_alloc_cache failed")
        dummy = b"\x00" * 32
        L.randomx_init_cache(self.cache, dummy, len(dummy))

        self.dataset = None
        if full_mem:
            self.dataset = L.randomx_alloc_dataset(flags)
            if not self.dataset:
                raise RuntimeError("randomx_alloc_dataset failed")

        print(f"[RandomX] flags=0x{flags:x}  full_mem={full_mem}")

    def set_seed(self, seed: bytes):
        """Re-init cache (and full dataset). Call only from the job thread."""
        with self.lock:
            if seed == self.current_seed:
                return
            print(f"[RandomX] new seed {seed.hex()[:16]}…")
            self.lib.randomx_init_cache(self.cache, seed, len(seed))
            if self.full_mem and self.dataset:
                count = self.lib.randomx_dataset_item_count()
                print("[RandomX] initializing dataset (30–90 s first time)…")
                t0 = time.time()
                self.lib.randomx_init_dataset(self.dataset, self.cache, 0, count)
                print(f"[RandomX] dataset ready in {time.time()-t0:.1f}s")
            self.current_seed = seed

    def create_vm(self):
        """Each mining thread must own its own VM."""
        vm = self.lib.randomx_create_vm(self.flags, self.cache, self.dataset)
        if not vm:
            raise RuntimeError("randomx_create_vm failed")
        return vm

    def destroy_vm(self, vm):
        if vm:
            self.lib.randomx_destroy_vm(vm)

    def hash(self, vm, data: bytes) -> bytes:
        out = (ctypes.c_ubyte * RANDOMX_HASH_SIZE)()
        self.lib.randomx_calculate_hash(vm, data, len(data), out)
        return bytes(out)

    def refresh_vm(self, vm):
        """After set_seed, point existing VMs at the new cache/dataset."""
        if self.full_mem and self.dataset:
            self.lib.randomx_vm_set_dataset(vm, self.dataset)
        else:
            self.lib.randomx_vm_set_cache(vm, self.cache)


def pack_nonce(blob: bytes, nonce: int) -> bytes:
    return blob[:39] + struct.pack("<I", nonce) + blob[43:]


# ---------- HTTP proxy client ----------

class PoolAPI:
    def __init__(self, base_url: str):
        # base_url is the full session prefix, e.g.
        #   http://127.0.0.1:8080/mysession/WALLET/password
        self.base = base_url.rstrip("/")
        self.s = requests.Session()

    def get_job(self):
        r = self.s.get(f"{self.base}/job", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_status(self):
        r = self.s.get(f"{self.base}/status", timeout=15)
        r.raise_for_status()
        return r.json()

    def submit(self, job_id, nonce_hex, result_hex, login_id=""):
        r = self.s.post(
            f"{self.base}/submit",
            json={
                "job_id": job_id,
                "nonce": nonce_hex,
                "result": result_hex,
                "login_id": login_id,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()


# ---------- shared mining state ----------

class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.job_id = None
        self.blob = None          # raw bytes
        self.target = 0
        self.login_id = ""
        self.height = 0
        self.version = 0          # bumped on every new job

    def update(self, job_id, blob_hex, target_hex, login_id, height):
        t = binascii.unhexlify(target_hex)
        if len(t) == 4:
            target = struct.unpack("<I", t)[0]
            if target == 0:
                target = 0xFFFFFFFF
            target = (0xFFFFFFFFFFFFFFFF // (0xFFFFFFFF // target)) if target else 0
        else:
            target = struct.unpack("<Q", t[:8])[0]

        with self.lock:
            self.job_id = job_id
            self.blob = binascii.unhexlify(blob_hex)
            self.target = target
            self.login_id = login_id or ""
            self.height = height
            self.version += 1
            return self.version

    def snapshot(self):
        with self.lock:
            if self.blob is None:
                return None
            return (
                self.job_id,
                self.blob,
                self.target,
                self.login_id,
                self.height,
                self.version,
            )


# ---------- worker thread ----------

def miner_worker(
    tid: int,
    engine: RandomXEngine,
    job_state: JobState,
    nonce_counter: list,          # [int] mutable shared counter
    nonce_lock: threading.Lock,
    stats: dict,
    stop_event: threading.Event,
    submit_q: queue.Queue,
):
    vm = engine.create_vm()
    local_hashes = 0
    last_version = -1

    try:
        while not stop_event.is_set():
            snap = job_state.snapshot()
            if snap is None:
                time.sleep(0.2)
                continue

            job_id, blob, target, login_id, height, version = snap

            if version != last_version:
                engine.refresh_vm(vm)
                last_version = version

            # grab a batch of nonces
            with nonce_lock:
                start = nonce_counter[0]
                nonce_counter[0] = (start + 256) & 0xFFFFFFFF
            end = start + 256

            for nonce in range(start, end):
                if stop_event.is_set():
                    break
                data = pack_nonce(blob, nonce & 0xFFFFFFFF)
                h = engine.hash(vm, data)
                local_hashes += 1

                r64 = struct.unpack("<Q", h[24:32])[0]
                if r64 < target:
                    hex_hash = binascii.hexlify(h).decode()
                    nonce_hex = binascii.hexlify(struct.pack("<I", nonce & 0xFFFFFFFF)).decode()
                    submit_q.put((job_id, nonce_hex, hex_hash, login_id, tid))

            stats[tid] = stats.get(tid, 0) + local_hashes
            local_hashes = 0
    finally:
        engine.destroy_vm(vm)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-thread RandomX miner via multi-session HTTP proxy"
    )
    parser.add_argument(
        "--api",
        required=True,
        help="Proxy session base URL, e.g. http://127.0.0.1:8080/mysession/WALLET/password",
    )
    parser.add_argument("--threads", type=int, default=0, help="Mining threads (0 = all CPUs)")
    parser.add_argument(
        "--full-mem",
        action="store_true",
        default=True,
        help="Full dataset mode (default on, much faster)",
    )
    parser.add_argument("--light", action="store_true", help="Force light mode (~256 MB)")
    parser.add_argument("--lib", default=None, help="Path to librandomx.so")
    args = parser.parse_args()

    full_mem = not args.light
    nthreads = args.threads if args.threads > 0 else max(1, cpu_count())

    print(f"CPUs detected: {cpu_count()}  →  using {nthreads} mining threads")
    print("Note: RandomX has no practical GPU backend — CPU only.")
    print(f"Proxy session URL: {args.api}")

    api = PoolAPI(args.api)
    engine = RandomXEngine(lib_path=args.lib, full_mem=full_mem)
    job_state = JobState()
    nonce_counter = [0]
    nonce_lock = threading.Lock()
    stats = {}
    stop_event = threading.Event()
    submit_q = queue.Queue()

    # start workers
    workers = []
    for tid in range(nthreads):
        t = threading.Thread(
            target=miner_worker,
            args=(tid, engine, job_state, nonce_counter, nonce_lock, stats, stop_event, submit_q),
            daemon=True,
        )
        t.start()
        workers.append(t)

    last_job_id = None
    started = time.time()
    total_hashes = 0
    last_stat_time = started

    print("Miner started — polling proxy for jobs…")

    try:
        while True:
            # ---- submit any found shares ----
            try:
                while True:
                    job_id, nonce_hex, hex_hash, login_id, tid = submit_q.get_nowait()
                    print(f"\n>>> SHARE thread={tid}  nonce={nonce_hex}")
                    try:
                        res = api.submit(job_id, nonce_hex, hex_hash, login_id)
                        print("    Submit OK:", res)
                    except Exception as e:
                        print("    Submit failed:", e)
            except queue.Empty:
                pass

            # ---- fetch job ----
            try:
                resp = api.get_job()
            except Exception as e:
                print("get_job failed:", e)
                time.sleep(2)
                continue

            if resp.get("status") != "ok":
                time.sleep(1)
                continue

            job = resp["job"]
            job_id = job["job_id"]
            blob = job["blob"]
            target_hex = job["target"]
            seed_hex = job.get("seed_hash")
            height = job.get("height", 0)
            login_id = job.get("login_id", "")

            if not seed_hex:
                time.sleep(1)
                continue

            if job_id != last_job_id:
                print(f"\nNew job  height={height}  target={target_hex}")
                last_job_id = job_id
                engine.set_seed(binascii.unhexlify(seed_hex))
                job_state.update(job_id, blob, target_hex, login_id, height)
                with nonce_lock:
                    nonce_counter[0] = 0

            # ---- hashrate report ----
            now = time.time()
            if now - last_stat_time >= 10:
                batch = sum(stats.values())
                for k in stats:
                    stats[k] = 0
                total_hashes += batch
                elapsed = now - started
                hr = int(total_hashes / elapsed) if elapsed > 0 else 0
                inst = int(batch / (now - last_stat_time)) if now > last_stat_time else 0
                print(f"[{inst} H/s instant | {hr} H/s avg]  total={total_hashes}  threads={nthreads}")
                last_stat_time = now

            time.sleep(0.3)

    except KeyboardInterrupt:
        print("\nStopping…")
        stop_event.set()
        for t in workers:
            t.join(timeout=2)


if __name__ == "__main__":
    main()
