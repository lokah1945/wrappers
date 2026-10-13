#!/usr/bin/env python3
"""Supervise production services selected by wrappers.json.

Usage: python start_wrappers.py [--wrapper NAME] [--list]
The registry starts first; pending provider flags cannot bypass their freeze.
"""
import argparse
import os
import sys
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from common.wrapper_scope import (
    REGISTRY, WrapperScopeError, frozen_wrappers, load_wrappers, require_production, select_services,
)


def configured_wrappers(requested=None):
    entries = select_services(requested, ROOT)
    selected = tuple(entries)
    # A new installation needs the dependency available before provider boot.
    selected = sorted(selected, key=lambda name: name != REGISTRY)
    return [{"name": name, "port": entries[name]["port"], "dir": str(ROOT / name),
             "module": entries[name]["module"], "entry_point": entries[name]["entry_point"]}
            for name in selected]


WRAPPERS = configured_wrappers()

processes = []

def start_wrapper(wrapper):
    if wrapper["name"] == 'dashboard':
        if 'dashboard' not in select_services(['dashboard'], ROOT):
            raise WrapperScopeError('dashboard is not enabled')
    else:
        require_production(wrapper["name"], ROOT)
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{wrapper['dir']}"
    
    cmd = [
        sys.executable, "-m", "uvicorn",
        wrapper["entry_point"],
        "--host", "127.0.0.1",
        "--port", str(wrapper["port"]),
        "--log-level", "info"
    ]
    
    log_file = open(f"/tmp/{wrapper['name']}.log", "a")
    try:
        proc = subprocess.Popen(cmd, cwd=wrapper["dir"], env=env, stdout=log_file, stderr=log_file)
    finally:
        log_file.close()
    return proc

def signal_handler(signum, frame):
    print("\nShutting down all wrappers...")
    for proc in processes:
        proc.terminate()
    sys.exit(0)

def main():
    global processes
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", action="append", help="Production provider plus registry; repeatable")
    parser.add_argument("--list", action="store_true", help="Print selected metadata without starting services")
    args = parser.parse_args()
    try:
        wrappers = configured_wrappers(args.wrapper)
    except WrapperScopeError as exc:
        parser.error(str(exc))
    for name in frozen_wrappers(ROOT):
        print(f"{name}: pending/frozen provider excluded without accessing files or runtime")
    if args.list:
        for wrapper in wrappers:
            print(f"{wrapper['name']}:{wrapper['port']}")
        return
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print("Starting production wrapper services and registry...")
    
    for wrapper in wrappers:
        print(f"Starting {wrapper['name']} on port {wrapper['port']}...")
        proc = start_wrapper(wrapper)
        processes.append(proc)
        print(f"  → PID {proc.pid}")
    
    # Write PID file
    with open("/tmp/wrapper-supervisor.pid", "w") as f:
        f.write(str(os.getpid()))
    
    print("\nAll wrappers started. Press Ctrl+C to stop.")
    
    # Wait for all processes
    try:
        for proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        signal_handler(None, None)

if __name__ == "__main__":
    main()
