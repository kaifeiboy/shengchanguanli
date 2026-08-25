namespace Platform.Core;

/// <summary>
/// 模块注册中心。集中管理所有已注册模块，并对外提供模块清单（前端据此自动渲染导航）。
/// 未来新增模块 = 实现 IModule + 在 Program.cs 调用 registry.Register(...)，平台零改动。
/// </summary>
public class ModuleRegistry
{
    private readonly List<IModule> _modules = new();

    public void Register(IModule module) => _modules.Add(module);

    public void RegisterServices(IServiceCollection services)
    {
        foreach (var m in _modules) m.RegisterServices(services);
    }

    public void MapEndpoints(WebApplication app)
    {
        foreach (var m in _modules.OrderBy(m => m.Order)) m.MapEndpoints(app);
    }

    /// <summary>前端导航数据源：GET /api/modules</summary>
    public object ListMeta() =>
        _modules.OrderBy(m => m.Order)
            .Select(m => new { m.Key, m.Name, m.Icon, m.Order })
            .ToList();
}
