# nginx 入门（给完全没接触过 nginx 的你）

## 一、nginx 是干什么的？

可以把 nginx 理解成一个“门口保安”：

- 你的 Web 管理台原本只听 `http://127.0.0.1:8456`（或者直接暴露 `0.0.0.0:8456`）；
- nginx 站在它前面，负责接收外部访问，再转发给 Web 管理台（这叫**反向代理**）；
- 好处：可以给访问加**域名**、加 **HTTPS 加密**、隐藏真实端口、统一管理证书。

```
你的浏览器
   │  https://report.example.com   （80/443，浏览器只看到这个）
   ▼
nginx（门口保安）  ← 校验域名、做 HTTPS 加密
   │  http://127.0.0.1:8456        （转发到本机 Web 管理台）
   ▼
web/app.py
```

**不是必须装的**：不装 nginx，直接 `http://服务器IP:8456` 也能用。
如果你不想学 nginx，跳过本文即可。装了之后体验更正规，也更安全。

---

## 二、装 nginx（Windows）

1. 到 nginx 官网下载 Windows 版 zip（`nginx-1.2x.x.zip`）；
2. 解压到 `C:\nginx\`；
3. 双击 `nginx.exe` 即启动（黑窗口一闪而过是正常的，它常驻后台）；
4. 浏览器打开 `http://127.0.0.1`，看到 “Welcome to nginx!” 就成功了。

---

## 三、最小配置：反向代理到 8456

用记事本打开 `C:\nginx\conf\nginx.conf`，在 `http { }` 块里加一段
`server { }`（文件里默认带一个 `listen 80` 的示例 server，可以整个替换成下面这样）：

```nginx
server {
    listen 80;
    server_name _;              # 用 IP 访问就写 _；有域名就写你的域名

    location / {
        proxy_pass http://127.0.0.1:8456;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

改完重载配置：

```bat
cd /d C:\nginx
nginx -t        # 先检查语法，看到 ok 再继续
nginx -s reload # 重新加载配置
```

然后访问 `http://127.0.0.1` 或 `http://服务器IP:80`，就进了 Web 管理台，
端口 8456 被隐藏了。

> 建议把 Web 管理台本身改回只听本机（`start_web.bat` 里
> `REPORT_WEB_HOST=127.0.0.1`），只有 nginx 能访问它，更安全。

---

## 四、加 HTTPS（加密，可选但推荐）

### 情况 1：有域名 + 有公网 IP（推荐，免费证书）

用 [certbot](https://certbot.eff.org/) 或你域名商的免费证书（如阿里云/腾讯云
免费证书）申请证书，拿到两个文件：

- 证书文件：`fullchain.pem`
- 私钥文件：`privkey.pem`

放到 `C:\nginx\conf\cert\`，然后 `server` 改成：

```nginx
server {
    listen 443 ssl;
    server_name report.example.com;          # 换成你的域名

    ssl_certificate     C:/nginx/conf/cert/fullchain.pem;
    ssl_certificate_key C:/nginx/conf/cert/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8456;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}

# 把 http 的 80 端口访问自动跳到 https
server {
    listen 80;
    server_name report.example.com;
    return 301 https://$host$request_uri;
}
```

重载后访问 `https://report.example.com`。

### 情况 2：没有域名（自签证书，只为加密）

用 PowerShell 生成自签证书并导出（一次性操作）：

```powershell
New-SelfSignedCertificate -DnsName "你的服务器IP或域名" `
  -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(5)
```

导出证书文件后用浏览器导入，或者干脆跳过 HTTPS——内网 + IP 端口访问
也可以接受。自签证书浏览器会提示“不安全”，需要手动信任，体验一般。

---

## 五、常见问题

**Q：`nginx -t` 报错？**
看提示的行号，多半是少了分号或括号。改回原样，别硬试。

**Q：改了配置没生效？**
`nginx -s reload` 一下；还不行就 `nginx -s stop` 再双击 `nginx.exe`。

**Q：浏览器能打开 nginx 的欢迎页，但代理到不了管理台？**
确认 Web 管理台本身在跑（本机访问 `http://127.0.0.1:8456/api/status` 有返回），
再确认 `proxy_pass` 的地址和端口没写错。

**Q：公网访问 80/443 被运营商封了？**
可以改用高位端口，例如 `listen 8443 ssl;`，访问 `https://IP:8443`；
或者干脆不折腾 nginx，直接用 8456 + 令牌。
