"""A tiny real Windows executable for tool lifecycle tests; no network."""
import os
from pathlib import Path
import shutil
import subprocess

SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
class Probe {
    static int Main(string[] args) {
        string exe = Process.GetCurrentProcess().MainModule.FileName;
        string mode = File.Exists(exe + ".mode") ? File.ReadAllText(exe + ".mode").Trim() : "ok";
        if (args.Length > 0 && args[0] == "--child") { Thread.Sleep(120000); return 0; }
        if (mode == "child") {
            Process child = Process.Start(new ProcessStartInfo(exe, "--child") { UseShellExecute=false, CreateNoWindow=true });
            File.WriteAllText(exe + ".pid", child.Id.ToString());
        }
        if (mode == "hang" || mode == "child") { Thread.Sleep(120000); return 0; }
        if (mode == "exit") { Console.Error.WriteLine("fixture failure"); return 7; }
        string name = Path.GetFileNameWithoutExtension(exe).ToLowerInvariant();
        string version = name == "ffmpeg" || name == "ffprobe" ? name + " version fixture-1.0" : "2026.10.19";
        if (mode == "wrong") version = "unrelated program";
        if (mode == "stderr") Console.Error.WriteLine(version); else Console.WriteLine(version);
        return 0;
    }
}
'''


def compile_fixture(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    compiler = Path(os.environ.get('WINDIR', 'C:\\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise RuntimeError('Local Windows C# fixture compiler unavailable')
    source = directory / 'ToolProbe.cs'
    source.write_text(SOURCE, encoding='utf-8')
    target = directory / 'probe.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(target), str(source)],
        check=True, capture_output=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return target


def copy_fixture(compiled, path, mode='ok'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(compiled, path)
    Path(str(path) + '.mode').write_text(mode, encoding='utf-8')
    return path
