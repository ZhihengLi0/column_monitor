# Windows电脑设置指南

在Windows那台电脑（172.31.255.10）上需要做一次配置，让树莓派能够读取PostgreSQL数据库。

## 方法：开放PostgreSQL远程访问（推荐）

**在Windows电脑上，以管理员身份打开PowerShell，依次运行：**

### 第一步：添加防火墙规则

```powershell
netsh advfirewall firewall add rule name="PostgreSQL-CS2" protocol=TCP dir=in localport=5432 action=allow remoteip=172.31.255.0/24
```

### 第二步：修改PostgreSQL配置允许远程连接

找到 `pg_hba.conf` 文件（通常在 `C:\Program Files\PostgreSQL\14\data\pg_hba.conf`），
用记事本打开，在文件末尾添加一行：

```
host    cs2     postgres        172.31.255.0/24         md5
```

### 第三步：修改 postgresql.conf 监听所有接口

找到 `C:\Program Files\PostgreSQL\14\data\postgresql.conf`，找到这行：
```
#listen_addresses = 'localhost'
```
改为：
```
listen_addresses = '*'
```

### 第四步：重启PostgreSQL服务

```powershell
Restart-Service -Name "postgresql*"
```

### 第五步：确认PostgreSQL密码

在Windows的PowerShell中运行：
```powershell
& 'C:\Program Files\PostgreSQL\14\bin\psql.exe' -U postgres -c "\du"
```
如果postgres用户没有密码，需要设置一个（之后填入树莓派的config.py）：
```powershell
& 'C:\Program Files\PostgreSQL\14\bin\psql.exe' -U postgres -c "ALTER USER postgres WITH PASSWORD 'your_password';"
```

---

## 配置完成后

回到树莓派，修改 `/home/cdms/bluefors_monitor/config.py` 中的：
```python
REMOTE_PG_PASSWORD = "your_password"   # 填入PostgreSQL密码
```

然后测试连接：
```bash
python3 /home/cdms/bluefors_monitor/sync.py
```

---

## 可选：通过WinRM（如果上面方法有问题）

以管理员身份在Windows PowerShell运行：
```powershell
winrm set winrm/config/service/auth '@{Basic="true"}'
winrm set winrm/config/service '@{AllowUnencrypted="true"}'
```
然后联系我们更新树莓派端的同步脚本。
