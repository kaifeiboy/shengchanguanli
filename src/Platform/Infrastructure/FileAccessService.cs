using System.IO;

namespace Platform.Infrastructure;

/// <summary>
/// 本地文件访问层（北极星能力内核）：以「虚拟路径 + 路径白名单」安全地暴露本机目录。
/// 对应需求 SR-02 / 以及长期目标「远程访问本地文件做数据交换」。
/// 当前默认白名单指向 E:\生产打标效果图；后续可扩展多个白名单根，用于文件交换模块。
/// </summary>
public class FileAccessService
{
    /// <summary>虚拟路径前缀 -> 物理根目录 的白名单映射</summary>
    private readonly Dictionary<string, string> _roots;

    public FileAccessService(IConfiguration config)
    {
        var root = config["Files:Root"] ?? @"E:\生产打标效果图";
        if (!Directory.Exists(root)) Directory.CreateDirectory(root);
        _roots = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["drawings"] = root
        };
    }

    /// <summary>列出图纸目录下所有 PDF（用于初始化图纸库）</summary>
    public IReadOnlyList<FileInfo> ListDrawings()
    {
        var dir = new DirectoryInfo(_roots["drawings"]);
        return dir.GetFiles("*.pdf", SearchOption.TopDirectoryOnly)
                   .OrderBy(f => f.Name, StringComparer.OrdinalIgnoreCase)
                   .ToList();
    }

    /// <summary>
    /// 将虚拟路径（如 "drawings/xxx.pdf"）解析为物理路径，并做穿越防护。
    /// 非法路径返回 null。
    /// </summary>
    public string? ResolvePhysical(string virtualPath)
    {
        if (string.IsNullOrWhiteSpace(virtualPath)) return null;

        var parts = virtualPath.Split('/', '\\');
        if (parts.Length < 2) return null;

        var prefix = parts[0];
        if (!_roots.TryGetValue(prefix, out var root)) return null;

        var relative = string.Join(Path.DirectorySeparatorChar, parts.Skip(1));
        var full = Path.GetFullPath(Path.Combine(root, relative));

        // 防护：最终路径必须仍在白名单根目录内
        if (!full.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase))
            return null;

        return full;
    }
}
