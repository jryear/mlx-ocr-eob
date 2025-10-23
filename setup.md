Yes. Mac-native first (Apple Vision + CoreML) is the correct move. Use Vision for speed and privacy; reserve VLMs (e.g., dots.ocr) for hard pages later.

# Setup — minimal, deterministic, Mac-native

## 0) Prereqs

* **macOS Sequoia (15) + Xcode 17+** to access the newest Vision APIs (document reading + tables). ([Apple][1])
* Apple Vision’s new **document reading / table extraction** lives under the Vision docs (WWDC25 “Read documents using the Vision framework”; `RecognizeDocumentsRequest`). ([Apple Developer][2])

Verify toolchain:

```bash
xcodebuild -version
swift --version
```

## 1) Create a Swift CLI that emits JSON/NDJSON

Rationale: Vision is a **system framework**; the most stable way to use the newest APIs (incl. tables) is a small **SwiftPM executable** you can call from anything (shell, Python, Rust).

```bash
mkdir vision-ocr-cli && cd vision-ocr-cli
swift package init --type executable
```

Edit **Package.swift** to target Sequoia and link Vision:

```swift
// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "vision-ocr-cli",
  platforms: [.macOS(.v15)],   // Sequoia
  targets: [
    .executableTarget(
      name: "vision-ocr-cli",
      linkerSettings: [.linkedFramework("Vision"), .linkedFramework("CoreGraphics"), .linkedFramework("ImageIO")]
    )
  ]
)
```

Replace **Sources/vision-ocr-cli/main.swift** with a lean, reliable baseline using **VNRecognizeTextRequest** (always available). It rasterizes PDFs to images, runs Vision OCR, and writes **NDJSON (one JSON per page)**. You can drop in the new `RecognizeDocumentsRequest` later when you’re on Xcode 17+ (see “Enable tables” below).

```swift
import Foundation
import Vision
import CoreGraphics
import ImageIO

struct Box: Codable { let x: CGFloat, y: CGFloat, w: CGFloat, h: CGFloat }
struct Line: Codable { let text: String, let confidence: Float, let box: Box }
struct PageResult: Codable {
    let page_index: Int
    let width: Int
    let height: Int
    let lines: [Line]
}

enum Err: Error { case loadFailed(String) }

func cgImages(from url: URL, dpi: CGFloat = 300) throws -> [(CGImage, Int, Int)] {
    var out: [(CGImage, Int, Int)] = []
    if url.pathExtension.lowercased() == "pdf",
       let doc = CGPDFDocument(url as CFURL) {
        let count = doc.numberOfPages
        for i in 1...count {
            guard let page = doc.page(at: i) else { continue }
            let box = page.getBoxRect(.mediaBox)
            let scale = dpi / 72.0
            let width  = Int(box.width  * scale)
            let height = Int(box.height * scale)
            let cs = CGColorSpaceCreateDeviceRGB()
            guard let ctx = CGContext(data: nil, width: width, height: height,
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
            else { throw Err.loadFailed("CGContext") }
            ctx.interpolationQuality = .high
            ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
            ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
            ctx.saveGState()
            ctx.translateBy(x: 0, y: CGFloat(height))
            ctx.scaleBy(x: scale, y: -scale)
            ctx.drawPDFPage(page)
            ctx.restoreGState()
            if let img = ctx.makeImage() { out.append((img, width, height)) }
        }
        return out
    }

    // Raster image
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil)
    else { throw Err.loadFailed("Image load") }
    return [(img, img.width, img.height)]
}

func ocrLines(in image: CGImage, pageIndex: Int) throws -> PageResult {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.revision = VNRecognizeTextRequestRevision3 // stable and fast on Apple Silicon
    req.usesLanguageCorrection = true
    req.recognitionLanguages = ["en-US"]

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([req])

    var lines: [Line] = []
    if let observations = req.results as? [VNRecognizedTextObservation] {
        for o in observations {
            guard let best = o.topCandidates(1).first else { continue }
            let bb = o.boundingBox // normalized
            let box = Box(x: bb.origin.x, y: bb.origin.y, w: bb.size.width, h: bb.size.height)
            lines.append(Line(text: best.string, confidence: best.confidence, box: box))
        }
    }
    return PageResult(page_index: pageIndex, width: image.width, height: image.height, lines: lines)
}

func main() throws {
    var args = CommandLine.arguments.dropFirst()
    guard let inPath = args.first else {
        fputs("usage: vision-ocr-cli <file.(pdf|png|jpg|tiff)> [--ndjson out.jsonl] [--dpi 300]\n", stderr)
        exit(2)
    }
    args = args.dropFirst()
    var outPath: String? = nil
    var dpi: CGFloat = 300
    while let a = args.first {
        if a == "--ndjson", let p = args.dropFirst().first { outPath = p; args = args.dropFirst(2); continue }
        if a == "--dpi", let d = args.dropFirst().first, let v = Double(d) { dpi = CGFloat(v); args = args.dropFirst(2); continue }
        args = args.dropFirst()
    }
    let url = URL(fileURLWithPath: inPath)
    let pages = try cgImages(from: url, dpi: dpi)
    let outFH: FileHandle = {
        if let p = outPath {
            FileManager.default.createFile(atPath: p, contents: nil)
            return try! FileHandle(forWritingTo: URL(fileURLWithPath: p))
        } else { return FileHandle.standardOutput }
    }()

    let encoder = JSONEncoder()
    for (idx, tup) in pages.enumerated() {
        let res = try ocrLines(in: tup.0, pageIndex: idx)
        let data = try encoder.encode(res)
        outFH.write(data)
        outFH.write("\n".data(using: .utf8)!)
    }
    try? outFH.close()
}

do { try main() } catch { fputs("error: \(error)\n", stderr); exit(1) }
```

Build & run:

```bash
swift build -c release
./.build/release/vision-ocr-cli sample.pdf --ndjson out.jsonl --dpi 300
```

**What you get now:** fast, on-device OCR with lines + confidences + boxes, per page, **NDJSON** for easy downstream parsing. This is the stable baseline. Apple’s text recognition request is documented here. ([Apple Developer][3])

## 2) Enable **tables/paragraphs** (when you’re on the new API)

Apple added a higher-level **document reading** API that groups text into paragraphs and **extracts tables**. In your CLI, gate it behind availability and switch the request:

* Replace the VN pipeline with `RecognizeDocumentsRequest` and traverse `DocumentObservation` → `paragraphs` / `tables` → cells.
* Keep output as NDJSON, adding a `tables: [...]` array (rows×cols with cell text + boxes).

Reference (WWDC25 docs + sample “Recognize tables within a document”). ([Apple Developer][2])

> Note: Apple’s docs are JS-rendered, but the public pages and third-party summaries confirm the API name and capability. ([Apple Developer][2])

## 3) Optional Python harness (Miniforge) — for your benchmarks only

If you want to orchestrate tests from Python while keeping OCR in Swift:

```bash
conda create -n eob-ocr python=3.11 -y
conda activate eob-ocr
pip install pandas pypdf pillow tqdm
```

Example harness calls the Swift CLI, collects timing, and writes metrics CSV. (Swift stays the source of truth; Python is just glue.)

## 4) CoreML footing (future-proof)

CoreML isn’t required for Vision, but install the tooling now so you can slot **dots.ocr (CoreML encoder + MLX decoder)** later:

```bash
pip install coremltools mlx-core
```

This aligns with the community recipe for dots.ocr on Apple Silicon and keeps you GPU-native when you escalate to VLM parsing. ([yasoob.me][4])

---

# Execution order (no fluff)

1. **Build the CLI** above; validate on 10–20 EOB pages.
2. When you confirm throughput, **toggle the new document API** (paragraphs + tables) and extend the JSON schema. ([Apple Developer][2])
3. Only after gaps show up (dense tables, multi-column) do you **route those pages to dots.ocr (CoreML/MLX)**.

# Reality check

* The document API’s table extraction is **native** now; your baseline should be **sub-second to a few seconds/page** depending on rasterization and page complexity. ([Apple Developer][2])
* Your earlier ~20 s/page came from a VLM path; the Vision baseline will be much faster on M-series and is the right first stage. ([Apple Developer][3])

# Residual risks

* New API names/types may differ slightly across early Xcode 17 seeds; keep the CLI modular so you can swap the request object with one file change. ([Apple Developer][2])
* Apple’s table grouping is new; expect edge cases on messy EOB scans and plan post-processing. ([Apple Developer][5])

