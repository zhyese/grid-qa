"""种子脚本：补充缺失主题文档（电容器/避雷器/电缆），闭环验证 recall 提升。

文档 <5000 字 → 走本地 bge embedding（不依赖百炼），向量化稳。
运行（后端在 8001）：python scripts/seed_extra.py
"""
import httpx

BASE = "http://127.0.0.1:8001"

DOCS = {
    "电容器组运行维护规程.txt": """电容器组是变电站无功补偿的核心设备，并联于母线用于提高功率因数、降低线损、稳定电压。电容器组投退操作必须严格遵循规程：投入前应检查电容器外观无渗漏油、无鼓肚，套管无闪络放电痕迹；确认串联电抗器和放电线圈状态正常。电容器组投入操作应先合隔离开关再合断路器，退出时先断断路器再拉开隔离开关，严禁带负荷拉合隔离开关。电容器组停电检修前必须充分放电，人工接触前应使用绝缘杆对电容器逐相放电并接地，防止残余电荷伤人。运行中应监视电容器电流不超过额定值，三相电流不平衡度不超过5%。电容器组常见故障包括：渗漏油（密封老化）、鼓肚（介质劣化产气）、套管闪络（表面污秽）、熔丝熔断（内部元件击穿）。发生电容器爆炸或着火应立即切断电源，用干粉灭火器扑救，禁止用水。电容器组日常巡视项目：检查有无异响、异味，记录运行电流和电压，检查环境温度不超过40℃。电容器组电抗器选型应考虑抑制谐波和限制合闸涌流。""",

    "避雷器在线监测装置运行规程.txt": """避雷器是输变电设备过电压保护的关键装置，用于限制雷电过电压和操作过电压，保护变压器等主设备绝缘。金属氧化物避雷器（MOA）在线监测装置实时监测避雷器的泄漏电流和动作次数，是判断避雷器健康状态的重要手段。避雷器在线监测装置主要由泄漏电流传感器、动作计数器和监测指示器组成。正常运行时避雷器泄漏电流为阻性电流和容性电流之和，阻性电流反映阀片老化程度。避雷器在线监测异常判断方法：当监测器指示的泄漏电流明显增大（超过初始值1.5倍）或阻性电流占总电流比例超过25%时，应判断避雷器阀片可能老化或受潮。避雷器动作计数器异常跳变（非雷雨季节频繁动作）可能指示避雷器内部故障或系统谐振。巡视时应记录避雷器在线监测装置读数，与历史数据比对，发现泄漏电流持续增长应安排停电测试。避雷器在线监测装置常见故障：计数器卡涩、指示器玻璃破裂、泄漏电流表针脱落。避雷器更换时应核对其额定电压和持续运行电压与系统匹配。避雷器预试项目包括绝缘电阻测试和直流1mA电压及75%该电压下泄漏电流测量。""",

    "电力电缆线路故障查找方法.txt": """电力电缆线路是城市配电网的主要供电方式，因其敷设于地下，故障查找难度较大。电缆线路常见故障类型包括：接地故障（单相或多相对地绝缘下降）、短路故障（相间绝缘击穿）、断线故障（导体开路）、闪络性故障（高阻间歇性放电）。电缆线路接地故障查找方法：首先用兆欧表测量绝缘电阻判断故障性质和相别，确定是低阻接地还是高阻接地。对于低阻接地故障，可采用电桥法（缪雷环线法）测距，原理是利用惠斯通电桥平衡测量故障点距离。对于高阻接地故障，需先用高压将故障点烧穿降为低阻，再用电桥法或脉冲法测距。电缆故障精确定位常用声测定理法：在故障点施加高压脉冲，故障点放电产生声音和电磁波，用声磁同步定点仪在地面精确定位。现代电缆故障查找广泛采用低压脉冲法（测断线和低阻）和二次脉冲法、弧反射法（测高阻故障）。电缆线路故障测距后应结合电缆路径仪确定电缆走向，沿路径定点。电缆故障查找应注意安全：故障测距施加高压时人员不得靠近电缆终端，精确定位前应确认电缆已停电并放电接地。电缆线路预防性试验包括耐压试验和绝缘电阻测量。""",
}


def main():
    c = httpx.Client(base_url=BASE, timeout=120)
    tok = c.post("/api/system/login", json={"username": "admin", "password": "admin123"}).json()["data"]["token"]
    H = {"Authorization": "Bearer " + tok}

    # 1) 上传
    uploaded = []
    for name, content in DOCS.items():
        files = {"files": (name, content.encode("utf-8"), "text/plain")}
        r = c.post("/api/document/upload", headers=H, files=files, data={"docType": "运维手册"})
        d = r.json().get("data", {})
        if name in d.get("successList", []):
            print(f"✓ 上传 {name}")
            uploaded.append(name)
        else:
            print(f"✗ 上传失败 {name}: {r.text[:120]}")

    if not uploaded:
        print("无文档上传成功，终止")
        return

    # 2) list 拿 docId（取最新上传的）
    import time
    time.sleep(0.5)
    lst = c.get("/api/document/list?size=20", headers=H).json()["data"]["list"]
    name2id = {x["docName"]: x["docId"] for x in lst}
    doc_ids = [name2id[n] for n in uploaded if n in name2id]

    # 3) 解析
    pr = c.post("/api/document/parse", headers=H, json={"docIds": doc_ids})
    print("解析:", pr.json().get("message"), "->", [x.get("chunkCount") for x in pr.json().get("data", [])])

    # 4) 向量化（每份，同步返回即入库；<5000字走 bge）
    for did in doc_ids:
        vr = c.post("/api/document/vector/generate", headers=H, json={"docId": did})
        vd = vr.json().get("data", {})
        print(f"  向量化 {vd.get('docId','')[:8]}.. 路由={vd.get('embeddingRoute')} 向量数={vd.get('vectorCount')}")
    print(f"\n✓ {len(doc_ids)} 份文档已向量化入库")


if __name__ == "__main__":
    main()
