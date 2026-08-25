namespace Platform.Core;

/// <summary>
/// 平台模块契约。新增一个业务模块只需实现该接口并在 Program.cs 注册，
/// 平台核心（导航、路由、文件层、网络层）无需改动。
/// 所有模块接口统一挂在 /api/{Key}/... 下。
/// </summary>
public interface IModule
{
    /// <summary>模块唯一键，用于路由前缀 /api/{Key}/</summary>
    string Key { get; }

    /// <summary>前端展示名称</summary>
    string Name { get; }

    /// <summary>前端展示图标（emoji 即可）</summary>
    string Icon { get; }

    /// <summary>导航排序，越小越靠前</summary>
    int Order { get; }

    /// <summary>向 DI 容器注册本模块所需的服务</summary>
    void RegisterServices(IServiceCollection services);

    /// <summary>注册本模块的 HTTP 端点</summary>
    void MapEndpoints(WebApplication app);
}
