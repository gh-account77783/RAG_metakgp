"""SCM entrypoint for one reviewed GraphMind service; requires retained Windows pywin32."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in (None, ''):
    # -I omits the script directory; use only the administrator-owned sibling assets.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tools.windows_service_files import validate_spec, environment
except ModuleNotFoundError:  # Installed operator assets are sibling scripts, not an importable package.
    from windows_service_files import validate_spec, environment


def child_environment(spec, inherited=None):
    """Discard operator/cloud/Python credentials and use only declared service inputs."""
    inherited = os.environ if inherited is None else inherited
    system = inherited.get('SystemRoot', inherited.get('SYSTEMROOT', r'C:\Windows'))
    result = {'SystemRoot': system, 'WINDIR': system,
              'PATH': str(Path(spec['command'][0]).parent) + os.pathsep + str(Path(system)/'System32'),
              'HOME': str(Path(spec['state_dir'])/'home'),
              'USERPROFILE': str(Path(spec['state_dir'])/'home'),
              'APPDATA': str(Path(spec['state_dir'])/'cache/AppData/Roaming'),
              'LOCALAPPDATA': str(Path(spec['state_dir'])/'cache/AppData/Local'),
              'TEMP': str(Path(spec['state_dir'])/'tmp'), 'TMP': str(Path(spec['state_dir'])/'tmp')}
    if spec['environment_file'] is not None:
        result.update(environment(json.loads(Path(spec['environment_file']).read_text(encoding='utf-8-sig'))))
    result.update(spec['environment'])
    # These never belong to an application service, even through a misconfigured environment file.
    if any(key in result for key in ('PYTHONPATH', 'PYTHONHOME', 'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET')):
        raise ValueError('Service environment contains a forbidden interpreter/admin override')
    return result


def run_service(spec):
    # Import only when dispatching on Windows; rendering/review needs no pywin32 or admin rights.
    import pywintypes
    import ntsecuritycon
    import servicemanager
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32process
    import win32service
    import win32serviceutil

    class NativeService(win32serviceutil.ServiceFramework):
        _svc_name_ = spec['name']
        _svc_display_name_ = spec['name']

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            job = process = thread = log = stdin = None
            allocated = False
            try:
                # A private console delivers CTRL+C to this job's children only. A Python
                # callback handles it in the host; unlike SetConsoleCtrlHandler(NULL, TRUE),
                # that callback's ignore policy is not inherited by child processes.
                win32console = __import__('win32console')
                try:
                    win32console.FreeConsole()
                except pywintypes.error:
                    pass  # SCM ordinarily starts without a console.
                win32console.AllocConsole()
                allocated = True
                handler = lambda event: event in (win32con.CTRL_C_EVENT, win32con.CTRL_BREAK_EVENT)
                win32api.SetConsoleCtrlHandler(handler, True)
                # b312's WCHAR* name parameter requires text; empty means unnamed.
                job = win32job.CreateJobObject(None, '')
                info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                info['BasicLimitInformation']['LimitFlags'] = (win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                                               win32job.JOB_OBJECT_LIMIT_JOB_MEMORY)
                info['JobMemoryLimit'] = spec['memory_bytes']
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
                env = child_environment(spec)
                for key in ('TEMP', 'HOME', 'APPDATA', 'LOCALAPPDATA'):
                    Path(env[key]).mkdir(parents=True, exist_ok=True)
                security = pywintypes.SECURITY_ATTRIBUTES()
                security.bInheritHandle = True
                # None requests the file's default descriptor (including parent ACLs),
                # rather than pywin32's initially blank descriptor without a DACL.
                security.SECURITY_DESCRIPTOR = None
                log = win32file.CreateFile(str(Path(spec['state_dir'])/(spec['role'] + '.log')),
                    ntsecuritycon.FILE_APPEND_DATA, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    security, win32con.OPEN_ALWAYS, win32con.FILE_ATTRIBUTE_NORMAL, None)
                stdin = win32file.CreateFile('NUL', win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, security,
                    win32con.OPEN_EXISTING, 0, None)
                startup = win32process.STARTUPINFO()
                startup.dwFlags = win32con.STARTF_USESTDHANDLES
                startup.hStdInput, startup.hStdOutput, startup.hStdError = stdin, log, log
                # Assign the suspended process before any code or descendant can execute.
                process, thread, _, _ = win32process.CreateProcess(None,
                    subprocess.list2cmdline(spec['command']), None, None, True,
                    win32con.CREATE_SUSPENDED | win32con.CREATE_UNICODE_ENVIRONMENT,
                    env, spec['cwd'], startup)
                win32job.AssignProcessToJobObject(job, process)
                win32process.ResumeThread(thread)
                thread.Close(); thread = None
                handles = [self.stop_event, process]
                outcome = win32event.WaitForMultipleObjects(handles, False, win32event.INFINITE)
                if outcome == win32event.WAIT_OBJECT_0:
                    deadline = time.monotonic() + spec['stop_timeout_seconds']
                    if spec['stop_command'] is not None:
                        # Vendor stop tools (e.g. pg_ctl) are bounded and never invoke a shell.
                        # Console shutdown is still attempted when the vendor command fails.
                        try:
                            subprocess.run(spec['stop_command'], env=env, cwd=spec['cwd'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=min(30, spec['stop_timeout_seconds']), check=True)
                        except (OSError, subprocess.SubprocessError):
                            pass
                    if win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT:
                        win32api.GenerateConsoleCtrlEvent(win32con.CTRL_C_EVENT, 0)
                    while win32event.WaitForSingleObject(process, 1000) == win32event.WAIT_TIMEOUT:
                        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
                        if time.monotonic() >= deadline:
                            servicemanager.LogErrorMsg(spec['name'] + ': graceful stop timed out; closing owned job')
                            win32job.TerminateJobObject(job, 1)
                            win32event.WaitForSingleObject(process, 10000)
                            raise RuntimeError('Owned service child required forced termination')
                    code = win32process.GetExitCodeProcess(process)
                    if code:
                        raise RuntimeError('Owned service child stopped with a failure: ' + str(code))
                    while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
                        if time.monotonic() >= deadline:
                            win32job.TerminateJobObject(job, 1)
                            raise RuntimeError('Owned service descendants required forced termination')
                        time.sleep(0.25)
                else:
                    # Do not silently report a crashed backend as a healthy SCM service.
                    code = win32process.GetExitCodeProcess(process)
                    servicemanager.LogErrorMsg(spec['name'] + ': child exited unexpectedly (' + str(code) + ')')
                    # ServiceFramework's dispatcher reports a nonzero SCM exit on exception.
                    raise RuntimeError('Owned service child exited unexpectedly: ' + str(code))
            finally:
                # Closing the job owns cleanup even after setup failure or a parent crash.
                # If assignment failed the child remains suspended; explicitly terminate it.
                if thread is not None and process is not None:
                    win32process.TerminateProcess(process, 1)
                for handle in (job, thread, process, log, stdin):
                    if handle is not None:
                        handle.Close()
                if allocated:
                    win32api.SetConsoleCtrlHandler(handler, False)
                    win32console.FreeConsole()

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(NativeService)
    servicemanager.StartServiceCtrlDispatcher()


def main():
    if sys.platform != 'win32' or len(sys.argv) != 2:
        raise SystemExit('This entrypoint requires Windows SCM and one protected specification path')
    spec = validate_spec(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    if spec['role'] in ('reader', 'worker') and (Path(sys.argv[1]).parent/'RESTORE_SEALED.json').exists():
        raise SystemExit('Restored reader/worker state is sealed pending canonical reconciliation')
    run_service(spec)


if __name__ == '__main__':
    main()
