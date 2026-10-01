from pathlib import Path
p=Path('web/globe3d.js');s=p.read_text(encoding='utf-8');s=s.replace("const L={'欧亚经典环线'", "const AMAP_KEY='7913f5e8e44f41933f72f18b5e9320d';const L={'欧亚经典环线'");s=s.replace("if(!window.AMAP_KEY)return;if(window.AMap)","if(!AMAP_KEY)return;if(window.AMap)").replace("encodeURIComponent(window.AMAP_KEY)","encodeURIComponent(AMAP_KEY)");p.write_text(s,encoding='utf-8')
