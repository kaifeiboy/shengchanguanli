using System.Diagnostics;

namespace Platform.Infrastructure;

public class FileAccessService
{
    public string? ResolvePhysical(string virtualPath) => null;
}

public interface IPythonProcessFactory
{
    string PythonExe { get; }
    string LocateScript(string fileName, params string[] searchPaths);
    ProcessStartInfo Create(string scriptPath, IEnumerable<string> args);
}
