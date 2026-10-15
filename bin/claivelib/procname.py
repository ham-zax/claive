"""Best-effort Linux process naming for claive supervisors."""
import os
import sys


# PR_SET_NAME behavior adapted from lop/local_operator/procname.py.
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.

PR_SET_NAME = 15


def restore_sys_executable():
    """Restore CPython's executable path after a Linux spawn with a descriptive argv[0]."""
    if sys.platform != "linux" or sys.executable:
        return
    try:
        executable = os.readlink("/proc/self/exe")
        if executable:
            sys.executable = executable
    except (OSError, ValueError):
        pass


def set_supervisor_process_name(job_id):
    """Set the Linux comm name to claive-<first eight ID chars>; never raise."""
    if sys.platform != "linux":
        return
    restore_sys_executable()
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        prctl = getattr(libc, "prctl", None)
        if prctl is None:
            return
        name = f"claive-{str(job_id)[:8]}".encode("ascii", "replace")[:15]
        buffer = ctypes.create_string_buffer(name)
        prctl(PR_SET_NAME, ctypes.byref(buffer), 0, 0, 0)
    except Exception:
        pass
