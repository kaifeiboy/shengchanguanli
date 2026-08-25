using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;

namespace Platform.Core;

/// <summary>
/// 网络访问层：负责解析本机在局域网内的 IP，并统一返回内网/外网 BaseUrl。
/// 对应需求 SR-02 / SR-03（内外网可达、前端地址自动识别）。
/// 说明：H5 通过同源相对路径访问 API，因此绝大多数情况下无需前端手动切换地址；
/// 该层主要用于展示"手机扫码/访问地址"以及填写外网隧道地址。
/// </summary>
public class NetworkResolver
{
    public string GetLanIp()
    {
        foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (ni.OperationalStatus != OperationalStatus.Up) continue;
            if (ni.NetworkInterfaceType is NetworkInterfaceType.Loopback) continue;

            var props = ni.GetIPProperties();
            if (props.GatewayAddresses.Count == 0) continue; // 无网关通常不是可用局域网

            foreach (var ua in props.UnicastAddresses)
            {
                if (ua.Address.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(ua.Address))
                    return ua.Address.ToString();
            }
        }
        return "127.0.0.1";
    }

    public object GetInfo(string port, string? externalBaseUrl)
    {
        var lan = GetLanIp();
        var ext = string.IsNullOrWhiteSpace(externalBaseUrl) ? null : externalBaseUrl;
        return new
        {
            lanIp = lan,
            port,
            internalBaseUrl = $"http://{lan}:{port}",
            externalBaseUrl = ext,
            hasExternal = ext != null
        };
    }
}
