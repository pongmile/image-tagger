# Image Tagger

โปรแกรมค้นหาและจัดหมวดหมู่รูปภาพบนเครื่องแบบ local-first: ค้นจากชื่อไฟล์ โฟลเดอร์ tag บุคคล ข้อความในภาพ และความหมายของภาพได้ โดยรูปไม่ถูกอัปโหลดออกจากเครื่อง

## ดาวน์โหลดและติดตั้ง

Release สำหรับผู้ใช้ทั่วไปเป็น Windows 64-bit สองแบบ:

- `Image-Tagger-<version>-win-x64.exe` — ตัวติดตั้ง แนะนำสำหรับผู้ใช้ทั่วไป
- `Image-Tagger-<version>-win-x64.zip` — portable แตก ZIP แล้วเปิด `Image Tagger.exe`

ดาวน์โหลดได้จาก [GitHub Releases](https://github.com/pongmile/image-tagger/releases/latest) โดยไม่ต้องติดตั้ง Node.js, Python, CUDA Toolkit หรือ Visual Studio

ติดตั้งแล้วเปิดโปรแกรม ไปที่ **Sources → Add folder → Rescan** จากนั้นเริ่มค้นหาได้ทันที ระบบ OCR พื้นฐานรวมอยู่ในโปรแกรม ส่วนโมเดล AI ขนาดใหญ่ติดตั้งเพิ่มจากหน้า **Models** ตามต้องการ

> Release ที่ build เองและยังไม่ได้ code-sign อาจแสดง Windows SmartScreen ให้ตรวจ SHA-256 ใน `SHA256SUMS.txt` ก่อนเปิด ห้ามปิด antivirus หรือดาวน์โหลด build จากแหล่งที่ไม่เชื่อถือ

## สเปกขั้นต่ำ

| ระดับ | CPU / RAM | พื้นที่ว่าง | GPU | เหมาะกับ |
|---|---:|---:|---|---|
| ขั้นต่ำ | x64 4 cores, RAM 8 GB | 2 GB + ขนาด thumbnail/index | ไม่ต้องมี | ค้นหา, manual tags, metadata, OCR |
| แนะนำ | x64 6 cores, RAM 16 GB | 10 GB | NVIDIA VRAM 6 GB หรือ CPU | WD14, CLIP, face detection |
| งานหนัก | x64 8+ cores, RAM 32 GB | 25 GB+ | NVIDIA VRAM 12 GB+ | โมเดล accurate/high-tier และ captioning |

- OS: Windows 10 22H2 หรือ Windows 11 แบบ 64-bit
- หน้าจอ: 1280×720 ขึ้นไป
- Internet: ใช้เฉพาะตอนดาวน์โหลดโมเดลเสริม
- ผู้ใช้ปลายทางไม่ต้องลง Node.js, Python, CUDA Toolkit หรือ Visual Studio

พื้นที่โมเดลขึ้นกับตัวเลือก: WD14 ประมาณ 0.3–1.4 GB, CLIP ประมาณ 0.34–3.9 GB, InsightFace 0.1–0.33 GB, BLIP 1–1.9 GB และ JoyCaption ประมาณ 16 GB (ควรมีพื้นที่เผื่อ cache อย่างน้อย 20 GB)

JoyCaption เป็นโมเดลเสริมแบบ opt-in และไม่ถูกเลือกอัตโนมัติ:

- `JoyCaption 4-bit`: Windows 11 x64, NVIDIA GPU รุ่น Pascal/GTX 10-series ขึ้นไป, VRAM ประมาณ 6 GB, RAM 16 GB+
- `JoyCaption full`: NVIDIA GPU ที่รองรับ BF16, VRAM ประมาณ 17 GB, RAM 32 GB+
- เครื่อง CPU-only ให้ใช้ BLIP; โปรแกรมจะไม่พยายามรัน JoyCaption บน CPU จนเครื่องค้าง

## วิธีใช้แบบเร็ว

1. เปิด **Sources** และเพิ่มโฟลเดอร์รูปด้วย **Add folder**
2. เพิ่ม exclude folder/pattern หากไม่ต้องการ index โฟลเดอร์ชั่วคราว
3. กด **Rescan** และรอแถบ indexing เป็น `idle`
4. ใช้ช่องค้นหา เช่น `beach`, `character:"hatsune miku"`, `folder:travel !draft`
5. เลือกรูปเพื่อดู preview, OCR, metadata, face และ tag; คลิกรูป preview เพื่อเปิดภาพใหญ่ ใช้ล้อเมาส์หรือปุ่ม +/− เพื่อซูม และลากเพื่อเลื่อนภาพ
6. เปิด **Models** เมื่อต้องการ WD14, semantic search, face recognition หรือ captioning

ตัวอย่างภาพที่แจกพร้อม repository อยู่ใน [`samples/`](samples/) สามารถเพิ่มโฟลเดอร์นี้เป็น Source เพื่อทดลองระบบได้

### Search syntax

| Query | ความหมาย |
|---|---|
| `cat dog` | ต้องพบทั้งสองคำ |
| `cat \| dog` | พบคำใดคำหนึ่ง |
| `!draft` หรือ `-draft` | ตัดผลลัพธ์ที่มีคำนี้ |
| `"blue sky"` | exact phrase |
| `character:miku` | tag ใน category `character` |
| `person:alice` | face cluster ที่ตั้งชื่อแล้ว |
| `folder:D:/Photos` | จำกัดโฟลเดอร์ |
| `size:>10mb` | จำกัดขนาดไฟล์ |
| `*.png` | wildcard |

คู่มือฉบับเต็ม: [`docs/USER_GUIDE_TH.md`](docs/USER_GUIDE_TH.md)

## ความสามารถ

- SQLite FTS5 trigram search; Node อ่านฐานข้อมูลโดยตรง ไม่ผ่าน Python
- รองรับ AND, OR, NOT, grouping, phrase, wildcard, regex, size/category/folder/person filters
- List/small/large thumbnail views พร้อม virtual scrolling
- Manual/bulk tags, custom categories, rename/merge tags และ few-shot learned tags
- OCR ไทย/อังกฤษ, EXIF/PNG metadata และ Stable Diffusion parameters
- WD14 สำหรับ anime tags, CLIP สำหรับ scene/clothing/pose และ semantic search
- InsightFace clustering/naming และ local caption model
- Background indexing, filesystem watcher, retry, pause/manual mode และ recovery เมื่อ daemon หยุด
- เก็บ database, thumbnail และ model ไว้ใน `%USERPROFILE%\.image-tagger` โดยค่าเริ่มต้น

## พัฒนาและทดสอบ

สิ่งที่ต้องมี:

- Node.js 24 LTS และ npm 10+
- Python 3.12 (รองรับ 3.10–3.12)
- Git
- `uv` เฉพาะตอนสร้าง bundled Python runtime
- Visual Studio Build Tools + workload “Desktop development with C++” เฉพาะกรณี native module ไม่มี prebuilt binary

```powershell
git clone https://github.com/pongmile/image-tagger.git
cd image-tagger
npm run setup
npm test
npm run dev
```

คำสั่งหลัก:

```powershell
npm run build:renderer  # production Angular build
npm run test:python     # 8 indexer/model pipeline suites
npm run test:electron   # better-sqlite3 + Python daemon integration
npm run test:ui         # hidden BrowserWindow UI regression + screenshot
npm run bench           # search performance gate
npm run dist:win        # NSIS installer + portable ZIP + packaged smoke test
```

รายละเอียด architecture และ build: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) และ [`docs/RELEASING.md`](docs/RELEASING.md)

## โครงสร้าง

```text
apps/desktop/             Electron main/preload + Angular renderer
apps/indexer/             Python indexing and local AI pipeline
packages/db/              canonical SQLite schema and migration
samples/                  redistributable example images
scripts/                  setup, test, benchmark and packaging tools
.github/workflows/        CI and tagged Windows release
```

## ข้อจำกัด

- Binary release ที่ตรวจสอบใน repository นี้เป็น Windows x64; macOS/Linux ต้อง build จาก source และจัด bundled Python runtime แยกตาม OS
- Video ใช้ browse/search ได้ แต่ยังไม่รัน AI tagging/captioning
- โมเดลขนาดใหญ่ไม่รวมใน installer และมี license ของแต่ละ upstream project
- ความแม่นยำเป็น probabilistic; ควรใช้ confidence filter และ confirm/reject เพื่อปรับ learned tags

## Privacy และ license

การ index และ inference ทำในเครื่อง ไม่มี telemetry หรือ image upload ในโค้ดปัจจุบัน ดูวิธีรายงานปัญหาความปลอดภัยที่ [`SECURITY.md`](SECURITY.md)

Source code ใช้ MIT License ดู [`LICENSE`](LICENSE) รูปตัวอย่างใน `samples/` สร้างขึ้นสำหรับ repository นี้และแจกภายใต้ MIT License เดียวกัน
