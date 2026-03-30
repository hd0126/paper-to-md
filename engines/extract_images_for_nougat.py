import fitz  # PyMuPDF
import io
from PIL import Image
from pathlib import Path

pdf_path = "/Users/hdkim/Documents/GitHub/Obsidian/_Inbox-Papers/s41467-017-02685-9.pdf"
output_dir = Path("/Users/hdkim/Documents/GitHub/Obsidian/_Inbox-Papers/s41467-017-02685-9_Nougat_assets")
output_dir.mkdir(exist_ok=True)

pdf_file = fitz.open(pdf_path)
for page_index in range(len(pdf_file)):
    page = pdf_file[page_index]
    image_list = page.get_images(full=True)
    
    for image_index, img in enumerate(image_list):
        xref = img[0]
        base_image = pdf_file.extract_image(xref)
        image_bytes = base_image["image"]
        image_ext = base_image["ext"]
        
        image = Image.open(io.BytesIO(image_bytes))
        # 특정 크기 이하의 노이즈 이미지(아이콘 등) 제외
        if image.width > 200 and image.height > 200:
            image_filename = f"page_{page_index+1}_img_{image_index+1}.{image_ext}"
            image.save(output_dir / image_filename)
            print(f"Saved: {image_filename}")

pdf_file.close()
