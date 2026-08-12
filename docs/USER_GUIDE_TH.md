# คู่มือผู้ใช้ Image Tagger

## 1. เริ่มใช้งานครั้งแรก

ติดตั้งจากไฟล์ `.exe` หรือแตก portable `.zip` แล้วเปิด `Image Tagger.exe` โปรแกรมเก็บข้อมูลของตัวเองไว้ที่ `%USERPROFILE%\.image-tagger` และไม่แก้ไขไฟล์รูปต้นฉบับ

1. ไปที่ **Sources**
2. กด **Add folder** แล้วเลือกโฟลเดอร์รูป
3. ถ้าต้องการข้ามบางโฟลเดอร์ ให้เพิ่ม exclude root หรือ pattern เช่น `**/node_modules/**`, `**/.git/**`, `*.tmp`
4. กด **Rescan this source** หรือกลับหน้า Search แล้วกด **Rescan**
5. รอจนสถานะเป็น `idle`

ใช้โฟลเดอร์ `samples/` จาก portable/repository เพื่อทดลองได้โดยไม่มีข้อมูลส่วนตัว

## 2. ค้นหา

เมื่อช่องค้นหาว่าง โปรแกรมแสดงรายการรูปที่ index แล้วสูงสุด 1,000 รายการ เมื่อพิมพ์คำค้น ผลลัพธ์เปลี่ยนแบบ live

- คำหลายคำเป็น AND: `beach sunset`
- OR: `beach | forest`
- NOT: `beach !draft`
- Group: `<beach|forest> vacation`
- Phrase: `"blue jacket"`
- Wildcard: `holiday_*.jpg`
- Category: `scene:beach`, `character:miku`, `tag:shirt`
- Person: `person:alice`
- Folder: `folder:D:/Photos/2026`
- Size: `size:>10mb`, `size:1mb-20mb`

ตัวเลือก **Match Case**, **Whole Word**, **Match Path**, **Diacritics** และ **Regex** ใช้ปรับความเข้มงวด หาก query ผิด โปรแกรมจะแสดง error ใต้ช่องค้นหาแทนการค้าง

## 3. มุมมองและการเลือก

- **List** เหมาะกับชื่อไฟล์และโฟลเดอร์จำนวนมาก
- **Small/Large** แสดง thumbnail แบบ virtualized
- คลิกเพื่อเลือก, `Shift+คลิก` เพื่อเลือกช่วง, ลากเพื่อ marquee select
- ดับเบิลคลิกหรือกด Enter เพื่อเปิดไฟล์ต้นฉบับ
- คลิกขวาเพื่อเปิดไฟล์, เปิดโฟลเดอร์, copy path, tag, re-index หรือดู properties

## 4. Tag และ learned tag

เพิ่ม tag ด้วยรูปแบบ `category:name` เช่น `project:website` หรือ `character:alice` เลือกหลายรูปเพื่อ bulk tag ได้

สำหรับ learned tag:

1. เพิ่ม tag เดียวกันให้รูปตัวอย่างที่ถูกต้องอย่างน้อย 5 รูป
2. กด train เพื่อสร้าง centroid จาก embedding
3. ใช้ ✓/✕ บน suggestion เพื่อ confirm/reject
4. เมื่อมี positive/negative examples มากขึ้น ระบบอัปเกรดเป็น linear classifier

Manual tag ไม่ถูกโมเดลเขียนทับ

## 5. Models

หน้า **Models** แสดง dependency, model file, variant, ขนาด และ hardware tier

- OCR: พร้อมใช้ใน base install; เหมาะกับไทย/อังกฤษ
- WD14: anime character/general/clothing/pose tags
- CLIP: scene/clothing/pose, semantic search และ learned-tag embedding
- InsightFace: ตรวจจับและจัดกลุ่มใบหน้าจริง
- Caption: สร้างคำอธิบายภาพเพื่อค้นหาด้วยภาษาธรรมชาติ

กดติดตั้ง dependency ก่อน แล้วดาวน์โหลด model/variant ที่แนะนำ การดาวน์โหลด resume ได้และเก็บไว้ใน models folder ที่เลือก โมเดลใหม่อาจต้อง **Re-index** เพื่อเติมข้อมูลให้รูปเดิม

## 6. People และ OCR

หน้า **People** ใช้ตั้งชื่อ face cluster และ merge cluster ที่เป็นคนเดียวกัน หลังตั้งชื่อค้นหาด้วย `person:name` ได้

ใน preview เปิด **Text in image (OCR)** เพื่อแก้ข้อความที่อ่านผิด เมื่อบันทึก FTS จะอัปเดตและค้นหาข้อความใหม่ได้ทันที

## 7. สำรองและย้ายเครื่อง

ปิดโปรแกรมก่อน copy โฟลเดอร์ `%USERPROFILE%\.image-tagger` ซึ่งประกอบด้วย:

- `library.db` — index, tags, settings
- `thumbs/` — thumbnail cache
- `models/` — model files หากไม่ได้เลือก drive อื่น
- `runtime-packages/` — optional Python dependencies

ไฟล์รูปต้นฉบับต้องอยู่ path เดิม หากเปลี่ยน drive/path ให้เพิ่ม Source ใหม่แล้ว Rescan

## 8. แก้ปัญหา

- ไม่พบรูป: ตรวจว่า Source enabled, pattern ไม่ exclude และกด Rescan
- Search ไม่เจอ tag ใหม่: รอ job เป็น `idle` หรือกด re-index เฉพาะรูป
- Semantic ใช้ไม่ได้: ติดตั้ง/เปิด CLIP และ re-index เพื่อสร้าง embeddings
- GPU ไม่ทำงาน: ตรวจ driver และหน้า Models; ระบบจะ fallback เป็น CPU
- Model download ล้มเหลว: ตรวจพื้นที่ว่าง/network แล้วกด retry; partial file ไม่ถูกมองว่าเป็น model ที่สมบูรณ์
- โปรแกรมเปิดซ้ำไม่ได้: เป็นพฤติกรรมตั้งใจเพื่อป้องกัน writer/process ซ้ำ หน้าต่างเดิมจะถูก focus
- ต้องการ reset: สำรองก่อน แล้วเปลี่ยนชื่อ `%USERPROFILE%\.image-tagger`; การลบโฟลเดอร์นี้จะลบ index/tag/settings ทั้งหมด แต่ไม่ลบรูปต้นฉบับ
