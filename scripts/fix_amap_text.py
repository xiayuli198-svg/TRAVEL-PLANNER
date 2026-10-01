from pathlib import Path
p=Path('web/globe3d.js');s=p.read_text(encoding='utf-8').replace("高德 3D 需要配置 API Key。请在页面控制台设置 window.AMAP_KEY 后再次点击。","正在加载高德 3D 地形…");p.write_text(s,encoding='utf-8')
