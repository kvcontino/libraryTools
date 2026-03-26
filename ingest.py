import os
import subprocess
import shutil
import re
from pathlib import Path
from tqdm import tqdm

# --- 1. CONFIGURATION ---
SOURCE_DIR = Path("~/6_reading").expanduser()
TARGET_DIR = Path("~/Library/Markdown").expanduser()

# --- 2. TOOLS (FUNCTIONS) ---

def verify_image_links(md_path, book_dir):
    image_dir = book_dir / "images"
    with open(md_path, 'r') as f:
        content = f.read()

    links = re.findall(r'!\[.*?\]\((.*?)\)', content)
    broken = []
    for link in links:
        img_file = (md_path.parent / link).resolve()
        if not img_file.exists():
            broken.append(link)

    if broken:
        print(f"  ⚠️  {len(broken)} missing images in {md_path.name}")
    else:
        print(f"  ✅ All image links verified.")

def process_pdf(fpath, book_dir):
    final_md = book_dir / f"{fpath.stem}.md"
    if final_md.exists(): final_md.unlink()
    
    # Ensure temp is clean before we start
    temp_out = book_dir / "temp"
    if temp_out.exists(): shutil.rmtree(temp_out)
    
    info = subprocess.check_output(["pdfinfo", str(fpath)]).decode()
    pages = int([line for line in info.split('\n') if "Pages:" in line][0].split()[1])
    
    chunk_size = 50
    pbar = tqdm(total=pages, desc=f"  📄 {fpath.stem[:20]}...", unit="pg", leave=False)
    
    for start in range(0, pages, chunk_size):
        end = min(start + chunk_size - 1, pages - 1)
        
        temp_out = book_dir / "temp"
        subprocess.run([
            "marker_single", str(fpath),
            "--output_dir", str(temp_out),
            "--page_range", f"{start}-{end}",
            "--pdftext_workers", "1"
        ], check=True)
        
        gen_folder = next(temp_out.iterdir())
        src_md = gen_folder / f"{gen_folder.name}.md"
        
        with open(src_md, "r") as src, open(final_md, "a") as dst:
            dst.write(src.read() + "\n\n")
            
        img_dst = book_dir / "images"
        img_dst.mkdir(exist_ok=True)
        img_src = gen_folder / "images"
        if img_src.exists():
            for img in img_src.iterdir():
                shutil.move(str(img), str(img_dst / img.name))
        
        shutil.rmtree(temp_out)
        
        # FIXED: Move update INSIDE the loop to see progress per chunk
        pbar.update(end - start + 1)
    
    pbar.close()
    verify_image_links(final_md, book_dir)

def process_ebook(fpath, book_dir):
    print(f"\n  -> Converting eBook via Pandoc: {fpath.name}")
    final_md = book_dir / f"{fpath.stem}.md"
    subprocess.run([
        "pandoc", str(fpath), 
        "-t", "commonmark", 
        "-o", str(final_md),
        f"--extract-media={book_dir}" 
    ], check=True)
    verify_image_links(final_md, book_dir)

# --- 3. MAIN LOGIC ---

def main(dry_run=True):
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    
    # Filter files first
    files_to_process = [f for f in SOURCE_DIR.iterdir() 
                        if f.suffix.lower() in ['.pdf', '.epub', '.mobi']]

    # FIXED: Single loop for both dry_run and execution
    for fpath in tqdm(files_to_process, desc="📚 Total Library Progress", disable=dry_run):
        if dry_run:
            method = 'Marker' if fpath.suffix.lower() == '.pdf' else 'Pandoc'
            print(f"[DRY RUN] Target: {fpath.name} | Method: {method}")
            continue
            
        book_dir = TARGET_DIR / fpath.stem
        book_dir.mkdir(exist_ok=True)
        
        if fpath.suffix.lower() == '.pdf':
            process_pdf(fpath, book_dir)
        else:
            process_ebook(fpath, book_dir)

# --- 4. EXECUTION SWITCH ---

if __name__ == "__main__":
    # Change to False when you're ready to commit your CPU to the task
    main(dry_run=False)
