import re
from pathlib import Path

def merge_nougat_and_images(mmd_path, asset_dir, output_path):
    with open(mmd_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Nougat의 이미지 플레이스홀더 패턴 (예: [MISSING_PAGE_EMPTY], [FIGURE_ID] 등)
    # 또한 "Figure 1", "Fig. 1" 텍스트를 찾아 이미지를 그 위에 삽입합니다.
    
    assets = sorted(list(Path(asset_dir).glob("*.jpeg")))
    img_idx = 0
    
    processed_content = []
    lines = content.split('\n')
    
    for line in lines:
        # "Fig. 1", "Figure 1" 등의 패턴을 찾으면 이미지 삽입
        if re.search(r'Fig\.?\s+\d+|Figure\s+\d+', line, re.IGNORECASE) and img_idx < len(assets):
            img_rel_path = f"{Path(asset_dir).name}/{assets[img_idx].name}"
            processed_content.append(f"\n![{line.strip()}]({img_rel_path})\n")
            img_idx += 1
        
        processed_content.append(line)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(processed_content))
    print(f"Final merged file created: {output_path}")

if __name__ == "__main__":
    mmd = "/Users/hdkim/Documents/GitHub/Obsidian/_Inbox-Papers/s41467-017-02685-9.mmd"
    assets = "/Users/hdkim/Documents/GitHub/Obsidian/_Inbox-Papers/s41467-017-02685-9_Marker_assets"
    out = "/Users/hdkim/Documents/GitHub/Obsidian/_Inbox-Papers/s41467-017-02685-9_Nougat_Final.md"
    
    if Path(mmd).exists():
        merge_nougat_and_images(mmd, assets, out)
    else:
        print("Waiting for MMD file...")
