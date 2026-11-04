"""Reproducible R19 offline regression: deny all sockets/DNS and telemetry."""
import os
from pathlib import Path
import socket
import sys
import threading

# Actual SSE disconnect tests need loopback TCP. This opt-in never permits the
# Provider proxy or external DNS; the historical default remains socket-free.
ALLOW_LOOPBACK = '--allow-loopback' in sys.argv
if ALLOW_LOOPBACK:
    sys.argv.remove('--allow-loopback')

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"

def denied(*args, **kwargs):
    raise AssertionError("R19 offline acceptance forbids network and DNS")

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_socketpair = socket.socketpair
original_getaddrinfo = socket.getaddrinfo
original_create_connection = socket.create_connection
internal_socketpair = threading.local()

def guarded_connect(sock, address):
    # Windows implements asyncio's local wakeup socketpair through loopback TCP.
    # Only this synchronous socketpair construction may use an ephemeral port.
    if ((ALLOW_LOOPBACK or getattr(internal_socketpair, "constructing", False)) and isinstance(address, tuple)
            and address[0] in {"127.0.0.1", "::1"} and address[1] != 7892):
        return original_connect(sock, address)
    return denied()

def guarded_connect_ex(sock, address):
    if ((ALLOW_LOOPBACK or getattr(internal_socketpair, "constructing", False)) and isinstance(address, tuple)
            and address[0] in {"127.0.0.1", "::1"} and address[1] != 7892):
        return original_connect_ex(sock, address)
    return denied()

def guarded_socketpair(*args, **kwargs):
    internal_socketpair.constructing = True
    try:
        return original_socketpair(*args, **kwargs)
    finally:
        internal_socketpair.constructing = False

socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
socket.socketpair = guarded_socketpair
def guarded_resolve(host,*args,**kwargs):
    if ALLOW_LOOPBACK and host in {'localhost','127.0.0.1','::1'}:
        return original_getaddrinfo(host,*args,**kwargs)
    return denied()

socket.create_connection = original_create_connection if ALLOW_LOOPBACK else denied
socket.getaddrinfo = guarded_resolve
socket.gethostbyname = denied

# A known legacy regression wrote into R6. Refuse historical eval writes in
# the test process; new R22 outputs and per-test databases remain writable.
import os
HISTORY = ROOT / "docs/eval"
DRAFT = HISTORY / "m6.2-r21-offline-review-r22-20261031"
def deny_historical_write(event, args):
    paths = []
    if event == "open":
        path, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
            paths = [path]
    elif event in {"os.rename", "os.remove", "os.rmdir"}:
        paths = list(args[:2] if event == "os.rename" else args[:1])
    for value in paths:
        if isinstance(value,(str,bytes)):
            path = Path(os.fsdecode(value)).resolve()
            if path.is_relative_to(HISTORY) and not path.is_relative_to(DRAFT):
                raise AssertionError("R22_TEST_REFUSES_HISTORICAL_EVAL_WRITE:" + path.relative_to(ROOT).as_posix())
sys.addaudithook(deny_historical_write)
import pytest
raise SystemExit(pytest.main(sys.argv[1:] or [
 "tests/test_m6_2_r22_review.py", "tests/test_m2_self_rag.py", "tests/test_m2_policy_execution.py",
 "tests/test_m2_routing.py", "tests/test_m3_4_policy_agent.py", "tests/test_m6_2_personal_dev_batch_r7.py", "tests/test_m6_2_r325_seven.py", "tests/test_m6_2_personal_dev_batch_r1.py", "tests/test_m6_2_review_r19.py", "tests/test_m6_2_metrics_offline.py", "tests/test_m6_2_r10_score_closeout.py", "tests/test_langgraph_g1.py", "tests/test_langgraph_g2.py",
 "-p", "pytest_workspace_tmp_path_r1", "-p", "no:cacheprovider",
 "--junitxml=docs/probes/m62-r22-offline-final.xml", "-q", "--tb=short"
]))
