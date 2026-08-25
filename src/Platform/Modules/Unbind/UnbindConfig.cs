namespace Platform.Modules.Unbind;

/// <summary>
/// 维修解绑模块配置。
/// 中转服务（Node.js proxy）负责把请求转发到目标内网系统 https://10.10.10.68:7443/，
/// 并注入 PC 浏览器已登录的 Session/Cookie，因此本模块只需要知道中转服务地址。
/// </summary>
public class UnbindConfig
{
    /// <summary>本地 Node.js 中转服务地址。默认 5001，避免与 .NET 后端 5000 冲突。</summary>
    public string ProxyBaseUrl { get; set; } = "http://localhost:5001";
}
