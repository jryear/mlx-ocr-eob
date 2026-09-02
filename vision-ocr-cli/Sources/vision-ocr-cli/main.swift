import Foundation
import Vision
import CoreGraphics
import ImageIO
import CryptoKit
import UniformTypeIdentifiers
import zlib

private let minimumTextHeight: Float = 0.015
private let routingVersion = "1.0"
private let latinLetterSet = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

private extension Unicode.Scalar {
    var isASCII: Bool { return value <= 0x7F }
}

struct Box: Codable {
    let x: CGFloat
    let y: CGFloat
    let w: CGFloat
    let h: CGFloat
}

struct PixelBox: Codable {
    let x: Int
    let y: Int
    let w: Int
    let h: Int
}

struct RegionBox: Codable {
    let normalized: Box
    let pixel: PixelBox
}

struct Line: Codable {
    let text: String
    let confidence: Float
    let box: Box
    let pixel_box: PixelBox
}

struct Paragraph: Codable {
    let text: String
    let confidence: Float?
    let box: Box
    let pixel_box: PixelBox
}

struct TableCell: Codable {
    let r: Int
    let c: Int
    let row_span: Int?
    let col_span: Int?
    let text: String
    let box: Box
    let pixel_box: PixelBox
    let confidence: Double?
}

struct Table: Codable {
    let id: String
    let rows: Int
    let cols: Int
    let box: Box
    let cells: [TableCell]
    var stats: TableStats
}

struct TableStats: Codable {
    let cell_count: Int
    let median_cell_conf: Double?
}

struct Region: Codable {
    let box: RegionBox
    let line_indices: [Int]
    let text: String
}

struct HeaderRegions: Codable {
    let left: Region?
    let right: Region?
}

struct PageRegions: Codable {
    let header: HeaderRegions?
}

struct Meta: Codable {
    let dpi: Int
    let revision: String
    let lang: [String]
    let duration_ms: Int
    let mean_confidence: Double?
    let line_density: Double?
    let line_spacing_var: Double?
    let column_count: Int?
    let line_count: Int
    let numeric_density: Double?
    let tau_conf: Double?
    let script_conflict_rate: Double?
    let script_conflict_rate_max: Double?
    let paragraph_median_conf: Double?
    let min_text_height: Double
    let crc32: String
    let timestamp: String
}

struct PageOutput: Codable {
    let schema_version: String
    let engine: String
    let mode: String
    let mode_fallback: Bool
    let source_path: String
    let page_index: Int
    let width: Int
    let height: Int
    let lines: [Line]
    let paragraphs: [Paragraph]?
    let tables: [Table]?
    let layout_text: String?
    let regions: PageRegions?
    let reading_order: [String]?
    let kv: [KeyValue]
    let meta: Meta
    let routing: RoutingInfo
    let non_content: Bool
}

enum Route: String, Codable {
    case text
    case vlm
    case skip
}

struct KeyValue: Codable {
    let key: String
    let value: String
    let source: String
    let confidence: Double?
    let box_px: PixelBox?
}

struct RoutingFeatures: Codable {
    let tau_conf: Double?
    let est_cols: Int?
    let numeric_density: Double?
    let script_conflict_rate: Double?
    let script_conflict_rate_max: Double?
    let paragraph_median_conf: Double?
    let median_cell_conf: Double?
}

struct RoutingInfo: Codable {
    let decision: Route
    let features: RoutingFeatures
    let minimum_text_height: Double
}

struct VLMROI: Codable {
    let px: Int
    let py: Int
    let pw: Int
    let ph: Int
}

struct VLMRequestEnvelope: Codable {
    let schema: String
    let doc_sha256: String
    let page_index: Int
    let roi_id: String
    let image_path: String
    let roi: VLMROI
    let hints: [String: Double]?
}

private struct PageMetrics {
    let durationMS: Int
    let crc32: String
    let meanConfidence: Double?
    let lineDensity: Double?
    let lineSpacingVar: Double?
    let columnCount: Int
    let lineCount: Int
    let numericDensity: Double?
    var tauConfidence: Double?
    var paragraphMedianConfidence: Double?
    var scriptConflictRate: Double?
    var scriptConflictRateMax: Double?
    var medianCellConfidence: Double?
    var isNonContent: Bool
    var route: Route?
}

private struct RoutingStats {
    private(set) var confidences: [Double] = []
    private(set) var lastTau: Double?

    mutating func nextTau(for confidence: Double?) -> Double {
        let value = confidence ?? 0.0
        confidences.append(value)
        let median = medianConfidence()
        let tau = clampTau((median ?? value) - 0.05)
        lastTau = tau
        return tau
    }

    func medianConfidence() -> Double? {
        guard !confidences.isEmpty else { return nil }
        let sorted = confidences.sorted()
        let mid = sorted.count / 2
        if sorted.count % 2 == 0 {
            return (sorted[mid - 1] + sorted[mid]) / 2.0
        } else {
            return sorted[mid]
        }
    }

    private func clampTau(_ value: Double) -> Double {
        return max(0.55, min(0.85, value))
    }

    mutating func recordLastTau(_ value: Double) {
        lastTau = value
    }
}

private struct PageImage {
    let index: Int
    let image: CGImage
    let width: Int
    let height: Int
}

struct CLIOptions {
    enum Mode: String {
        case text
        case document
    }

    struct PageSlice {
        let start: Int
        let end: Int?

        func range(clampingTo upperBound: Int) throws -> Range<Int> {
            let minStart = max(0, start)
            let resolvedEnd = min(end ?? upperBound, upperBound)
            guard minStart < resolvedEnd else {
                throw CLIError.usage("pages slice produces empty range")
            }
            return minStart..<resolvedEnd
        }
    }

    let inputPath: String
    let inputURL: URL
    let outputURL: URL?
    let dpi: Int
    let mode: Mode
    let languages: [String]
    let pages: PageSlice?
    let timeoutMS: Int?
    let logRunURL: URL?
    let emitDebugDir: URL?
    let vlmRunner: String?
    let vlmExportDir: URL?
    let vlmPreprocess: Bool
    let vlmPreprocessScript: String?
    let forceVLM: String?
    let exportImagesDir: URL?

    static func parse(_ arguments: [String]) throws -> CLIOptions {
        guard arguments.count > 1 else {
            throw CLIError.usage("""
            usage: vision-ocr-cli <file.(pdf|png|jpg|tiff)> [--ndjson out.jsonl] [--dpi 300] [--pages start:end] [--timeout ms] [--log-run path] [--mode text|document] [--lang codes] [--emit-debug-overlays dir] [--vlm-runner path] [--vlm-export dir] [--vlm-preprocess] [--vlm-preprocess-script path] [--force-vlm [both|headers|data]] [--export-images dir]
            """)
        }

        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        let inputPath = arguments[1]
        let inputURL = URL(fileURLWithPath: inputPath, relativeTo: cwd).standardizedFileURL

        var outputURL: URL?
        var dpi = 300
        var mode: Mode = .document
        var languages: [String] = ["en-US"]
        var pages: PageSlice?
        var timeoutMS: Int?
        var logRunURL: URL?
        var emitDebugDir: URL?
        var vlmRunner: String?
        var vlmExportDir: URL?
        var vlmPreprocess: Bool = false
        var vlmPreprocessScript: String?
        var forceVLM: String? = nil
        var exportImagesDir: URL? = nil

        var index = 2
        while index < arguments.count {
            let arg = arguments[index]
            switch arg {
            case "--ndjson":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--ndjson requires a path")
                }
                outputURL = URL(fileURLWithPath: arguments[index + 1], relativeTo: cwd).standardizedFileURL
                index += 2
            case "--dpi":
                guard index + 1 < arguments.count, let value = Int(arguments[index + 1]), value > 0 else {
                    throw CLIError.usage("--dpi requires a positive integer")
                }
                dpi = value
                index += 2
            case "--pages":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--pages requires start:end")
                }
                pages = try parsePageSlice(arguments[index + 1])
                index += 2
            case "--timeout":
                guard index + 1 < arguments.count, let value = Int(arguments[index + 1]), value > 0 else {
                    throw CLIError.usage("--timeout requires a positive integer (milliseconds)")
                }
                timeoutMS = value
                index += 2
            case "--log-run":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--log-run requires a path")
                }
                logRunURL = URL(fileURLWithPath: arguments[index + 1], relativeTo: cwd).standardizedFileURL
                index += 2
            case "--mode":
                guard index + 1 < arguments.count, let parsed = Mode(rawValue: arguments[index + 1]) else {
                    throw CLIError.usage("--mode must be 'text' or 'document'")
                }
                mode = parsed
                index += 2
            case "--lang":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--lang requires at least one language code")
                }
                let codes = arguments[index + 1]
                    .split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                if !codes.isEmpty {
                    languages = codes
                }
                index += 2
            case "--emit-debug-overlays":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--emit-debug-overlays requires a directory path")
                }
                emitDebugDir = URL(fileURLWithPath: arguments[index + 1], relativeTo: cwd).standardizedFileURL
                index += 2
            case "--vlm-runner":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--vlm-runner requires a path")
                }
                vlmRunner = arguments[index + 1]
                index += 2
            case "--vlm-export":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--vlm-export requires a directory path")
                }
                vlmExportDir = URL(fileURLWithPath: arguments[index + 1], relativeTo: cwd).standardizedFileURL
                index += 2
            case "--vlm-preprocess":
                vlmPreprocess = true
                index += 1
            case "--vlm-preprocess-script":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--vlm-preprocess-script requires a path")
                }
                vlmPreprocessScript = arguments[index + 1]
                index += 2
            case "--force-vlm":
                // Optional scope value; default to "both"
                var scope = "both"
                if index + 1 < arguments.count {
                    let next = arguments[index + 1]
                    if !next.hasPrefix("--") {
                        scope = next
                        index += 1
                    }
                }
                guard ["both","headers","data"].contains(scope) else {
                    throw CLIError.usage("--force-vlm optional value must be one of: both|headers|data")
                }
                forceVLM = scope
                index += 1
            case "--export-images":
                guard index + 1 < arguments.count else {
                    throw CLIError.usage("--export-images requires a directory path")
                }
                exportImagesDir = URL(fileURLWithPath: arguments[index + 1], relativeTo: cwd).standardizedFileURL
                index += 2
            default:
                throw CLIError.usage("unrecognized argument: \(arg)")
            }
        }

        let normalizedLangs = languages.compactMap(normalizeLanguageCode)
        let finalLangs = normalizedLangs.isEmpty ? ["en-US"] : normalizedLangs

        return CLIOptions(
            inputPath: inputPath,
            inputURL: inputURL,
            outputURL: outputURL,
            dpi: dpi,
            mode: mode,
            languages: finalLangs,
            pages: pages,
            timeoutMS: timeoutMS,
            logRunURL: logRunURL,
            emitDebugDir: emitDebugDir,
            vlmRunner: vlmRunner,
            vlmExportDir: vlmExportDir,
            vlmPreprocess: vlmPreprocess,
            vlmPreprocessScript: vlmPreprocessScript,
            forceVLM: forceVLM,
            exportImagesDir: exportImagesDir
        )
    }

    private static func parsePageSlice(_ value: String) throws -> PageSlice {
        let parts = value.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 2 else {
            throw CLIError.usage("--pages expects start:end (either may be blank)")
        }
        let startString = String(parts[0])
        let endString = String(parts[1])

        let start = startString.isEmpty ? 0 : (Int(startString) ?? -1)
        if start < 0 {
            throw CLIError.usage("invalid --pages start value")
        }

        let end = endString.isEmpty ? nil : Int(endString)
        if let end, end <= start {
            throw CLIError.usage("--pages end must be greater than start")
        }

        return PageSlice(start: start, end: end)
    }
}

private func retagFile(original: URL, newURL: URL) -> URL {
    if original.path == newURL.path { return original }
    let fm = FileManager.default
    do {
        if fm.fileExists(atPath: newURL.path) {
            try fm.removeItem(at: newURL)
        }
        try fm.moveItem(at: original, to: newURL)
        return newURL
    } catch {
        return original
    }
}

struct RunManifest: Codable {
    struct PageLog: Codable {
        let page_index: Int
        let crc32: String
        let duration_ms: Int
        let mean_confidence: Double?
        let tau_conf: Double?
        let line_count: Int
        let route: String?
        let non_content: Bool?
    }

    struct RoutingManifest: Codable {
        let version: String
        let tau_conf: Double?
        let median_confidence: Double?
        let features: [String: Bool]
    }

    struct VLMRun: Codable {
        let page_index: Int
        let roi_id: String
        let image_path: String
        let request_path: String
        let response_path: String?
        let request_crc32: String
        let response_crc32: String?
        let runner_status: String
        let preprocess_ops: [String]?
        let request_sha256: String?
        let response_sha256: String?
    }

    let timestamp: String
    let argv: [String]
    let macOS: String
    let swift_version: String?
    let xcode_version: String?
    let cli_git_sha: String?
    let input_path: String
    let input_sha256: String
    let ndjson_path: String
    let ndjson_crc32: String?
    let ndjson_line_count: Int?
    let schema_version: String
    let dpi: Int
    let mode: String
    let mode_fallback: Bool
    let lang: [String]
    let timeout_ms: Int?
    let page_count: Int
    let duration_ms: Int
    let engine: String
    let revision: String
    let min_text_height: Double
    let routing: RoutingManifest
    let vlm_runs: [VLMRun]?
    let pages: [PageLog]
    // Extended (optional)
    let runner_mode: String?
    struct ModelFingerprint: Codable {
        let name: String?
        let revision_sha: String?
        let quant: String?
        let tokenizer_sha: String?
        let chat_template_sha: String?
    }
    let model_fingerprint: ModelFingerprint?
    struct CachePaths: Codable { let HF_HOME: String?; let TRANSFORMERS_CACHE: String? }
    let cache_paths: CachePaths?
    struct SessionStats: Codable { let started_ts: String?; let ended_ts: String?; let roi_count: Int?; let avg_sec_per_roi: Double?; let p95_sec_per_roi: Double? }
    let session: SessionStats?
}

enum CLIError: Error {
    case usage(String)
    case load(String)
    case vision(String)
    case timeout
    case output(String)
}

private struct EngineInfo {
    let engine: String
    let revision: String
}

private func exportPNG(image: CGImage, to url: URL) throws {
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        throw CLIError.output("failed to create image destination for \(url.path)")
    }
    CGImageDestinationAddImage(dest, image, nil)
    if !CGImageDestinationFinalize(dest) {
        throw CLIError.output("failed to write PNG to \(url.path)")
    }
}

@main
struct VisionOCRCLI {
    static func main() async {
        do {
            try await run()
            exit(0)
        } catch CLIError.usage(let message) {
            fputs("\(message)\n", stderr)
            exit(2)
        } catch CLIError.load(let message) {
            fputs("load error: \(message)\n", stderr)
            exit(3)
        } catch CLIError.vision(let message) {
            fputs("vision error: \(message)\n", stderr)
            exit(4)
        } catch CLIError.timeout {
            fputs("error: timeout exceeded\n", stderr)
            exit(5)
        } catch CLIError.output(let message) {
            fputs("output error: \(message)\n", stderr)
            exit(6)
        } catch {
            fputs("error: \(error)\n", stderr)
            exit(1)
        }
    }

    private static func run() async throws {
        let options = try CLIOptions.parse(CommandLine.arguments)
        // Safety latch for --force-vlm
        if let scope = options.forceVLM {
            let env = ProcessInfo.processInfo.environment
            if env["ALLOW_FORCE_VLM"] != "1" {
                throw CLIError.usage("--force-vlm requires ALLOW_FORCE_VLM=1 in the environment for safety")
            }
            if options.vlmRunner == nil {
                throw CLIError.usage("--force-vlm requires --vlm-runner to be provided")
            }
            fputs("info: forcing VLM route (scope=\(scope))\n", stderr)
        }
        let supportsDocument = ProcessInfo.processInfo.isOperatingSystemAtLeast(OperatingSystemVersion(majorVersion: 26, minorVersion: 0, patchVersion: 0))
        var processingMode = options.mode
        var modeFallback = false
        if options.mode == .document && !supportsDocument {
            processingMode = .text
            modeFallback = true
        }
        let schemaVersion = "1.2"
        let engineInfo = processingMode == .document
            ? EngineInfo(engine: "Vision.Document", revision: "RecognizeDocumentsRequestRevision1")
            : EngineInfo(engine: "Vision", revision: "VNRecognizeTextRequestRevision3")

        if options.mode == .document && !supportsDocument {
            fputs("info: Document API unavailable, falling back to text mode\n", stderr)
        }

        if let overlayDir = options.emitDebugDir {
            try FileManager.default.createDirectory(at: overlayDir, withIntermediateDirectories: true, attributes: nil)
        }

        let pageImages = try loadImages(from: options.inputURL, dpi: options.dpi, slice: options.pages)
        guard !pageImages.isEmpty else {
            throw CLIError.load("no pages available for processing")
        }

        let docSHA = try sha256Digest(for: options.inputURL)

        var vlmExportDir: URL? = nil
        if options.vlmRunner != nil || options.vlmExportDir != nil {
            let baseDir = options.vlmExportDir ?? URL(fileURLWithPath: "runs/vlm", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
            try FileManager.default.createDirectory(at: baseDir, withIntermediateDirectories: true, attributes: nil)
            vlmExportDir = baseDir
        }

        let outputHandle = try prepareOutputHandle(at: options.outputURL)
        let isStdout = options.outputURL == nil
        defer {
            if !isStdout {
                try? outputHandle.close()
            }
        }

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        let isoFormatter = ISO8601DateFormatter()
        isoFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

        let runStart = DispatchTime.now()
        var pageLogs: [RunManifest.PageLog] = []
        var overlayEmitted = 0
        let overlayLimit = 3
        var routingStats = RoutingStats()
        var vlmLogs: [RunManifest.VLMRun] = []
        // Track NDJSON stream properties for manifest provenance
        var ndjsonCRC: uLong = 0
        var ndjsonLineCount: Int = 0

        // Persistent session metrics
        var sessionStartedAtISO: String? = nil
        var sessionEndedAtISO: String? = nil
        var roiDurations: [Double] = []
        var modelFP: RunManifest.ModelFingerprint? = nil

        for page in pageImages {
            if let exportDir = options.exportImagesDir {
                try FileManager.default.createDirectory(at: exportDir, withIntermediateDirectories: true, attributes: nil)
                let fileURL = exportDir.appendingPathComponent("\(docSHA)_p\(page.index).png")
                try exportPNG(image: page.image, to: fileURL)
            }
        let (payload, metrics) = try await buildPagePayload(
            page: page,
            options: options,
            engineInfo: engineInfo,
            schemaVersion: schemaVersion,
            isoFormatter: isoFormatter,
            routingStats: &routingStats,
            processingMode: processingMode,
            modeFallback: modeFallback,
            docSHA: docSHA,
                vlmRunner: options.vlmRunner,
                vlmExportDir: vlmExportDir,
                vlmLogs: &vlmLogs,
                sessionStartedISO: &sessionStartedAtISO,
                sessionEndedISO: &sessionEndedAtISO,
                roiDurations: &roiDurations,
                modelFingerprint: &modelFP
            )

            let data = try encoder.encode(payload)
            outputHandle.write(data)
            outputHandle.write(Data([0x0A]))
            // Incremental CRC32 over NDJSON stream
            data.withUnsafeBytes { buffer in
                if let base = buffer.bindMemory(to: UInt8.self).baseAddress, buffer.count > 0 {
                    ndjsonCRC = crc32(ndjsonCRC, base, uInt(buffer.count))
                }
            }
            var newline: UInt8 = 0x0A
            withUnsafeBytes(of: &newline) { ptr in
                if let base = ptr.baseAddress?.assumingMemoryBound(to: UInt8.self) {
                    ndjsonCRC = crc32(ndjsonCRC, base, 1)
                }
            }
            ndjsonLineCount += 1

            pageLogs.append(
                RunManifest.PageLog(
                    page_index: payload.page_index,
                    crc32: metrics.crc32,
                    duration_ms: metrics.durationMS,
                    mean_confidence: metrics.meanConfidence,
                    tau_conf: metrics.tauConfidence,
                    line_count: metrics.lineCount,
                    route: metrics.route?.rawValue,
                    non_content: metrics.isNonContent ? true : nil
                )
            )

            if let overlayDir = options.emitDebugDir, overlayEmitted < overlayLimit {
                let overlayURL = overlayDir
                    .appendingPathComponent(String(format: "page_%03d.png", payload.page_index))
                try renderDebugOverlay(
                    for: page,
                    lines: payload.lines,
                    paragraphs: payload.paragraphs,
                    tables: payload.tables,
                    to: overlayURL
                )
                overlayEmitted += 1
            }

            if let timeout = options.timeoutMS {
                let elapsed = DispatchTime.now().uptimeNanoseconds - runStart.uptimeNanoseconds
                if elapsed > UInt64(timeout) * 1_000_000 {
                    throw CLIError.timeout
                }
            }
        }

        let runEnd = DispatchTime.now()
        let totalDuration = Int(Double(runEnd.uptimeNanoseconds - runStart.uptimeNanoseconds) / 1_000_000.0)
        let logURL = try resolveLogURL(preferred: options.logRunURL, schemaVersion: schemaVersion)
        let manifest = try makeRunManifest(
            schemaVersion: schemaVersion,
            options: options,
            pageLogs: pageLogs,
            totalDuration: totalDuration,
            sha256: docSHA,
            engineInfo: engineInfo,
            logPath: logURL,
            routingStats: routingStats,
            processingMode: processingMode,
            modeFallback: modeFallback,
            vlmRuns: vlmLogs,
            ndjsonCRC32: String(format: "%08x", ndjsonCRC),
            ndjsonLineCount: ndjsonLineCount,
            runnerMode: (options.vlmRunner?.hasSuffix("dots-ocrd.py") == true) ? "persistent" : (options.vlmRunner != nil ? "per_roi" : nil),
            modelFingerprint: modelFP,
            cachePaths: RunManifest.CachePaths(HF_HOME: ProcessInfo.processInfo.environment["HF_HOME"], TRANSFORMERS_CACHE: ProcessInfo.processInfo.environment["TRANSFORMERS_CACHE"]),
            sessionStats: computeSessionStats(startISO: sessionStartedAtISO, endISO: sessionEndedAtISO, durations: roiDurations)
        )

        let manifestData = try JSONEncoder().encode(manifest)
        try manifestData.write(to: logURL, options: .atomic)
    }

    private static func buildPagePayload(
        page: PageImage,
        options: CLIOptions,
        engineInfo: EngineInfo,
        schemaVersion: String,
        isoFormatter: ISO8601DateFormatter,
        routingStats: inout RoutingStats,
        processingMode: CLIOptions.Mode,
        modeFallback: Bool,
        docSHA: String,
        vlmRunner: String?,
        vlmExportDir: URL?,
        vlmLogs: inout [RunManifest.VLMRun],
        sessionStartedISO: inout String?,
        sessionEndedISO: inout String?,
        roiDurations: inout [Double],
        modelFingerprint: inout RunManifest.ModelFingerprint?
    ) async throws -> (PageOutput, PageMetrics) {
        let pageStart = DispatchTime.now()
        let crc32 = crc32Hex(for: page.image)
        let lines: [Line]
        var paragraphs: [Paragraph]? = nil
        var tables: [Table]? = nil

        switch processingMode {
        case .text:
            lines = try performTextRecognition(on: page.image, languages: options.languages)
        case .document:
            if #available(macOS 26.0, *) {
                let doc = try await performDocumentRecognition(on: page.image, languages: options.languages)
                lines = doc.lines
                paragraphs = doc.paragraphs.isEmpty ? nil : doc.paragraphs
                var refinedTables = doc.tables
                if !refinedTables.isEmpty {
                    refinedTables = try refineTablesWithVision(
                        image: page.image,
                        tables: refinedTables,
                        pageWidth: page.width,
                        pageHeight: page.height
                    )
                }
                tables = refinedTables.isEmpty ? nil : refinedTables
            } else {
                throw CLIError.vision("--mode document requires macOS 26 or newer")
            }
        }

        let durationNano = DispatchTime.now().uptimeNanoseconds - pageStart.uptimeNanoseconds
        let durationMS = Int(Double(durationNano) / 1_000_000.0)
        var metrics = computeMetrics(
            lines: lines,
            paragraphs: paragraphs,
            tables: tables,
            width: page.width,
            height: page.height,
            durationMS: durationMS,
            crc32: crc32
        )
        // Update rolling statistics with mean line confidence
        _ = routingStats.nextTau(for: metrics.meanConfidence)
        // Spec: tau = clamp(0.55..0.85, paragraph_median_conf - 0.05),
        // fallback to rolling median of mean line confidence
        let tauBase = metrics.paragraphMedianConfidence ?? routingStats.medianConfidence() ?? (metrics.meanConfidence ?? 0.0)
        let tau = max(0.55, min(0.85, tauBase - 0.05))
        metrics.tauConfidence = tau
        routingStats.recordLastTau(tau)
        decideRoute(for: &metrics, tau: tau)
        let timestamp = isoFormatter.string(from: Date())
        let layoutText = makeLayoutText(from: lines)
        let regions = computeRegions(lines: lines, pageWidth: page.width, pageHeight: page.height)

        let meta = Meta(
            dpi: options.dpi,
            revision: engineInfo.revision,
            lang: options.languages,
            duration_ms: metrics.durationMS,
            mean_confidence: metrics.meanConfidence,
            line_density: metrics.lineDensity,
            line_spacing_var: metrics.lineSpacingVar,
            column_count: metrics.columnCount == 0 ? nil : metrics.columnCount,
            line_count: metrics.lineCount,
            numeric_density: metrics.numericDensity,
            tau_conf: metrics.tauConfidence,
            script_conflict_rate: metrics.scriptConflictRate,
            script_conflict_rate_max: metrics.scriptConflictRateMax,
            paragraph_median_conf: metrics.paragraphMedianConfidence,
            min_text_height: Double(minimumTextHeight),
            crc32: metrics.crc32,
            timestamp: timestamp
        )

        let routingFeatures = RoutingFeatures(
            tau_conf: metrics.tauConfidence,
            est_cols: metrics.columnCount,
            numeric_density: metrics.numericDensity,
            script_conflict_rate: metrics.scriptConflictRate,
            script_conflict_rate_max: metrics.scriptConflictRateMax,
            paragraph_median_conf: metrics.paragraphMedianConfidence,
            median_cell_conf: metrics.medianCellConfidence
        )
        var decision = metrics.route ?? .text
        if options.forceVLM != nil {
            decision = .vlm
        }
        let routingInfo = RoutingInfo(
            decision: decision,
            features: routingFeatures,
            minimum_text_height: Double(minimumTextHeight)
        )

        let readingOrder = makeReadingOrder(paragraphs: paragraphs, tables: tables)

        var payload = PageOutput(
            schema_version: schemaVersion,
            engine: engineInfo.engine,
            mode: processingMode.rawValue,
            mode_fallback: modeFallback,
            source_path: options.inputPath,
            page_index: page.index,
            width: page.width,
            height: page.height,
            lines: lines,
            paragraphs: paragraphs,
            tables: tables,
            layout_text: layoutText,
            regions: regions,
            reading_order: readingOrder,
            kv: [],
            meta: meta,
            routing: routingInfo,
            non_content: metrics.isNonContent
        )

        // Emit per-page sidecars (schema-safe, out-of-band)
        writeRasterSidecar(docSHA: docSHA, pageIndex: page.index, width: page.width, height: page.height, dpi: options.dpi)
        // Preproc sidecar compatible with preproc.page.v1.json
        let lineConfs: [Double] = lines.map { Double($0.confidence) }
        let pageMedianConf = median(of: lineConfs)
        // Header median: collect confidences from header regions (left/right) if present
        var headerMedianConf: Double? = nil
        if let header = regions?.header {
            var headerConfs: [Double] = []
            if let left = header.left {
                for idx in left.line_indices { if idx >= 0 && idx < lines.count { headerConfs.append(Double(lines[idx].confidence)) } }
            }
            if let right = header.right {
                for idx in right.line_indices { if idx >= 0 && idx < lines.count { headerConfs.append(Double(lines[idx].confidence)) } }
            }
            headerMedianConf = median(of: headerConfs)
        }
        // ROI classifications for scheduler (header + tables)
        let roiClassifications = makeROIClassifications(regions: regions, tables: tables, lines: lines, pageWidth: page.width, pageHeight: page.height)
        // Deterministic route reason (string code) based on gating
        let routeReason = computeRouteReason(decision: decision, metrics: metrics, tau: metrics.tauConfidence ?? 0.55)

        writePreprocSidecar(
            docSHA: docSHA,
            pageIndex: page.index,
            width: page.width,
            height: page.height,
            dpi: options.dpi,
            pageConfMedian: pageMedianConf,
            headerConfMedian: headerMedianConf,
            decision: decision,
            features: routingFeatures,
            roiClassifications: roiClassifications,
            routeReason: routeReason
        )
        // Compute and write a stable features SHA for audit without touching schema
        writePreprocFeaturesSHA(docSHA: docSHA, pageIndex: page.index, roiClassifications: roiClassifications)

        if decision == .vlm {
            try processVLM(
                page: page,
                payload: &payload,
                tables: tables,
                docSHA: docSHA,
                vlmRunner: vlmRunner,
                exportDir: vlmExportDir,
                features: routingFeatures,
                preprocess: options.vlmPreprocess,
                preprocessScript: options.vlmPreprocessScript,
                vlmLogs: &vlmLogs,
                sessionStartedISO: &sessionStartedISO,
                sessionEndedISO: &sessionEndedISO,
                roiDurations: &roiDurations,
                modelFingerprint: &modelFingerprint
            )
        }

        return (payload, metrics)
    }

    private static func performTextRecognition(on image: CGImage, languages: [String]) throws -> [Line] {
        let imageWidth = image.width
        let imageHeight = image.height
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.revision = VNRecognizeTextRequestRevision3
        request.usesLanguageCorrection = true
        request.recognitionLanguages = languages
        request.minimumTextHeight = minimumTextHeight

        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])

        guard let observations = request.results else {
            return []
        }

        return observations.compactMap { observation -> Line? in
            guard let best = observation.topCandidates(1).first else {
                return nil
            }
            let rect = observation.boundingBox
            let box = Box(x: rect.origin.x, y: rect.origin.y, w: rect.size.width, h: rect.size.height)
            let pixel = pixelBox(from: rect, imageWidth: imageWidth, imageHeight: imageHeight)
            return Line(text: best.string, confidence: best.confidence, box: box, pixel_box: pixel)
        }
    }

    @available(macOS 26.0, *)
    private static func performDocumentRecognition(on image: CGImage, languages: [String]) async throws -> (lines: [Line], paragraphs: [Paragraph], tables: [Table]) {
        let imageWidth = image.width
        let imageHeight = image.height
        var request = RecognizeDocumentsRequest()
        request.textRecognitionOptions.useLanguageCorrection = true
        request.textRecognitionOptions.automaticallyDetectLanguage = false
        var languageObjects = languages.compactMap { Locale.Language(identifier: $0) }
        if languageObjects.isEmpty {
            languageObjects = [Locale.Language(identifier: "en-US")]
        }
        request.textRecognitionOptions.recognitionLanguages = languageObjects
        request.textRecognitionOptions.maximumCandidateCount = 1
        request.textRecognitionOptions.minimumTextHeightFraction = minimumTextHeight

        let handler = ImageRequestHandler(image, orientation: nil)
        let observations = try await handler.perform(request)
        guard let document = observations.first?.document else {
            return ([], [], [])
        }

        let lines: [Line] = document.text.lines.compactMap { observation -> Line? in
            guard let best = observation.topCandidates(1).first else {
                return nil
            }
            let normalizedRect = observation.boundingBox
            let rect = CGRect(
                x: normalizedRect.origin.x,
                y: normalizedRect.origin.y,
                width: normalizedRect.width,
                height: normalizedRect.height
            )
            let box = Box(x: rect.origin.x, y: rect.origin.y, w: rect.width, h: rect.height)
            let pixel = pixelBox(from: rect, imageWidth: imageWidth, imageHeight: imageHeight)
            return Line(text: best.string, confidence: best.confidence, box: box, pixel_box: pixel)
        }

        let paragraphs: [Paragraph] = document.paragraphs.map { paragraph in
            let normalizedRect = paragraph.boundingRegion.boundingBox
            let rect = CGRect(
                x: normalizedRect.origin.x,
                y: normalizedRect.origin.y,
                width: normalizedRect.width,
                height: normalizedRect.height
            )
            let pixel = pixelBox(from: rect, imageWidth: imageWidth, imageHeight: imageHeight)
            let paragraphLines = paragraph.lines.compactMap { $0.topCandidates(1).first?.confidence }
            let meanConfidence = paragraphLines.isEmpty ? nil : paragraphLines.reduce(0, +) / Float(paragraphLines.count)
            return Paragraph(
                text: paragraph.transcript,
                confidence: meanConfidence,
                box: Box(x: rect.origin.x, y: rect.origin.y, w: rect.width, h: rect.height),
                pixel_box: pixel
            )
        }

        var tableIndex = 0
        let tables: [Table] = document.tables.map { table in
            defer { tableIndex += 1 }
            let rectNorm = table.boundingRegion.boundingBox
            let rowsCount = table.rows.count
            let colsCount = table.columns.count
            var cells: [TableCell] = []
            for (rowIdx, row) in table.rows.enumerated() {
                for (colIdx, cell) in row.enumerated() {
                    let normalizedCellRect = cell.content.boundingRegion.boundingBox
                    let cellCGRect = CGRect(
                        x: normalizedCellRect.origin.x,
                        y: normalizedCellRect.origin.y,
                        width: normalizedCellRect.width,
                        height: normalizedCellRect.height
                    )
                    let spanRows = cell.rowRange.upperBound - cell.rowRange.lowerBound + 1
                    let spanCols = cell.columnRange.upperBound - cell.columnRange.lowerBound + 1
                    let text = cell.content.text.transcript
                    let cellPixel = pixelBox(from: cellCGRect, imageWidth: imageWidth, imageHeight: imageHeight)
                    let cellLineConfidences = cell.content.text.lines.compactMap { $0.topCandidates(1).first?.confidence }
                    let cellConfidence = cellLineConfidences.isEmpty ? nil : Double(cellLineConfidences.reduce(0, +)) / Double(cellLineConfidences.count)
                    cells.append(
                        TableCell(
                            r: rowIdx,
                            c: colIdx,
                            row_span: spanRows > 1 ? spanRows : nil,
                            col_span: spanCols > 1 ? spanCols : nil,
                            text: text,
                            box: Box(
                                x: cellCGRect.origin.x,
                                y: cellCGRect.origin.y,
                                w: cellCGRect.width,
                                h: cellCGRect.height
                            ),
                            pixel_box: cellPixel,
                            confidence: cellConfidence
                        )
                    )
                }
            }
            let cellCount = cells.count
            let expected = rowsCount * colsCount
            if cellCount < expected {
                fputs("warning: table \(tableIndex) cell count < rows*cols (\(cellCount) < \(expected))\n", stderr)
            }
            let medianCell = median(of: cells.compactMap { $0.confidence })
            let stats = TableStats(cell_count: cellCount, median_cell_conf: medianCell)
            return Table(
                id: "t\(tableIndex)",
                rows: rowsCount,
                cols: colsCount,
                box: Box(x: rectNorm.origin.x, y: rectNorm.origin.y, w: rectNorm.width, h: rectNorm.height),
                cells: cells,
                stats: stats
            )
        }

        return (lines, paragraphs, tables)
    }

    private static func computeMetrics(
        lines: [Line],
        paragraphs: [Paragraph]?,
        tables: [Table]?,
        width: Int,
        height: Int,
        durationMS: Int,
        crc32: String
    ) -> PageMetrics {
        let lineCount = lines.count
        let meanConfidence = lines.isEmpty ? nil : Double(lines.reduce(0.0) { $0 + Double($1.confidence) }) / Double(lineCount)
        let pixelArea = Double(width * height)
        let lineDensity = pixelArea > 0 ? Double(lineCount) / (pixelArea / 1_000_000.0) : nil
        let columnCount = estimateColumnCount(from: lines)
        let lineSpacingVar = estimateLineSpacingVariance(from: lines)
        let numericDensity = estimateNumericDensity(from: lines)
        let paragraphMedian = paragraphMedianConfidence(paragraphs: paragraphs)
        let scriptStats = scriptConflictStats(paragraphs: paragraphs, fallbackLines: lines)
        let medianCellConf = medianCellConfidence(tables: tables)
        return PageMetrics(
            durationMS: durationMS,
            crc32: crc32,
            meanConfidence: meanConfidence,
            lineDensity: lineDensity,
            lineSpacingVar: lineSpacingVar,
            columnCount: columnCount,
            lineCount: lineCount,
            numericDensity: numericDensity,
            tauConfidence: nil,
            paragraphMedianConfidence: paragraphMedian,
            scriptConflictRate: scriptStats?.median,
            scriptConflictRateMax: scriptStats?.max,
            medianCellConfidence: medianCellConf,
            isNonContent: false,
            route: nil
        )
    }

    private static func decideRoute(for metrics: inout PageMetrics, tau: Double) {
        let confidence = metrics.meanConfidence ?? 0.0
        let density = metrics.lineDensity ?? 0.0
        let numericDensity = metrics.numericDensity ?? 0.0
        let paragraphMedian = metrics.paragraphMedianConfidence ?? confidence
        let scriptRate = metrics.scriptConflictRate ?? 0.0
        let scriptRateMax = metrics.scriptConflictRateMax ?? scriptRate
        let medianCell = metrics.medianCellConfidence ?? 1.0

        let isBlank = metrics.lineCount <= 3 && density < 0.30 && paragraphMedian < 0.45

        if isBlank {
            metrics.isNonContent = true
            metrics.route = .skip
            return
        }

        metrics.isNonContent = false

        if scriptRateMax > 0.05 {
            metrics.route = .vlm
            return
        }

        if metrics.columnCount >= 5 && (medianCell < 0.70 || numericDensity >= 0.55 || paragraphMedian < tau) {
            metrics.route = .vlm
            return
        }

        metrics.route = .text
    }

    private static func estimateColumnCount(from lines: [Line]) -> Int {
        guard !lines.isEmpty else { return 0 }
        let centers = lines.map { Double($0.box.x + ($0.box.w / 2.0)) }.sorted()
        let threshold = 0.12
        var clusters: [Double] = []
        for center in centers {
            if let last = clusters.last, abs(center - last) < threshold {
                continue
            }
            clusters.append(center)
        }
        return max(1, clusters.count)
    }

    private static func estimateLineSpacingVariance(from lines: [Line]) -> Double? {
        guard lines.count > 1 else { return nil }
        let centers = lines.map { Double($0.box.y + ($0.box.h / 2.0)) }.sorted(by: >)
        var deltas: [Double] = []
        for idx in 1..<centers.count {
            deltas.append(abs(centers[idx - 1] - centers[idx]))
        }
        guard !deltas.isEmpty else { return nil }
        let mean = deltas.reduce(0, +) / Double(deltas.count)
        let variance = deltas.reduce(0) { $0 + pow($1 - mean, 2) } / Double(deltas.count)
        return variance
    }

    private static func estimateNumericDensity(from lines: [Line]) -> Double? {
        guard !lines.isEmpty else { return nil }
        var digitCount = 0
        var totalCount = 0
        for line in lines {
            for scalar in line.text.unicodeScalars {
                if CharacterSet.whitespacesAndNewlines.contains(scalar) {
                    continue
                }
                totalCount += 1
                if CharacterSet.decimalDigits.contains(scalar) {
                    digitCount += 1
                }
            }
        }
        guard totalCount > 0 else { return nil }
        return Double(digitCount) / Double(totalCount)
    }

    private static func scriptConflictRate(for text: String) -> Double? {
        var letters = 0
        var conflicts = 0
        for scalar in text.unicodeScalars {
            if CharacterSet.letters.contains(scalar) {
                letters += 1
                if !latinLetterSet.contains(scalar) {
                    conflicts += 1
                }
            }
        }
        guard letters > 0 else { return nil }
        return Double(conflicts) / Double(letters)
    }

    private static func scriptConflictStats(paragraphs: [Paragraph]?, fallbackLines: [Line]) -> (median: Double, max: Double)? {
        var values: [Double] = []
        if let paragraphs, !paragraphs.isEmpty {
            for paragraph in paragraphs {
                if let rate = scriptConflictRate(for: paragraph.text) {
                    values.append(rate)
                }
            }
        }
        if values.isEmpty {
            let combined = fallbackLines.map(\.text).joined(separator: "\n")
            if let rate = scriptConflictRate(for: combined) {
                values.append(rate)
            }
        }
        guard !values.isEmpty else { return nil }
        let medianValue = median(of: values) ?? values[0]
        let maxValue = values.max() ?? medianValue
        return (medianValue, maxValue)
    }

    private static func paragraphMedianConfidence(paragraphs: [Paragraph]?) -> Double? {
        guard let paragraphs, !paragraphs.isEmpty else { return nil }
        let values = paragraphs.compactMap { paragraph -> Double? in
            guard let conf = paragraph.confidence else { return nil }
            return Double(conf)
        }
        return median(of: values)
    }

    private static func medianCellConfidence(tables: [Table]?) -> Double? {
        guard let tables else { return nil }
        var values: [Double] = []
        for table in tables {
            if let conf = table.stats.median_cell_conf {
                values.append(conf)
            }
        }
        return median(of: values)
    }

    private static func median(of values: [Double]) -> Double? {
        guard !values.isEmpty else { return nil }
        let sorted = values.sorted()
        let mid = sorted.count / 2
        if sorted.count % 2 == 1 {
            return sorted[mid]
        }
        if mid > 0 {
            return (sorted[mid - 1] + sorted[mid]) / 2.0
        }
        return sorted[0]
    }

    private static func makeReadingOrder(paragraphs: [Paragraph]?, tables: [Table]?) -> [String]? {
        var entries: [(Double, String)] = []
        if let paragraphs {
            for (index, paragraph) in paragraphs.enumerated() {
                let top = Double(paragraph.box.y + paragraph.box.h)
                entries.append((top, "para:\(index)"))
            }
        }
        if let tables {
            for (index, table) in tables.enumerated() {
                let top = Double(table.box.y + table.box.h)
                entries.append((top, "table:\(index)"))
            }
        }
        guard !entries.isEmpty else { return nil }
        return entries.sorted { $0.0 > $1.0 }.map { $0.1 }
    }

    private static func refineTablesWithVision(
        image: CGImage,
        tables: [Table],
        pageWidth: Int,
        pageHeight: Int
    ) throws -> [Table] {
        var updatedTables = tables
        for index in updatedTables.indices {
            let baseline = updatedTables[index].stats.median_cell_conf ?? 0.0
            if baseline >= 0.70 {
                continue
            }
            let normalizedRect = CGRect(
                x: updatedTables[index].box.x,
                y: updatedTables[index].box.y,
                width: updatedTables[index].box.w,
                height: updatedTables[index].box.h
            )
            let pixel = pixelBox(from: normalizedRect, imageWidth: pageWidth, imageHeight: pageHeight)
            guard let roiImage = try makeROIImage(image: image, pixel: pixel, scale: 1.15) else {
                continue
            }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.revision = VNRecognizeTextRequestRevision3
            request.usesLanguageCorrection = true
            request.minimumTextHeight = max(0.001, minimumTextHeight * 0.7)
            let handler = VNImageRequestHandler(cgImage: roiImage, options: [:])
            try handler.perform([request])
            guard let observations = request.results else { continue }
            let confidences = observations.compactMap { $0.topCandidates(1).first?.confidence }.map(Double.init)
            guard let newMedian = median(of: confidences) else { continue }
            if newMedian - baseline >= 0.04 {
                updatedTables[index].stats = TableStats(
                    cell_count: updatedTables[index].stats.cell_count,
                    median_cell_conf: newMedian
                )
            }
        }
        return updatedTables
    }

    private static func processVLM(
        page: PageImage,
        payload: inout PageOutput,
        tables: [Table]?,
        docSHA: String,
        vlmRunner: String?,
        exportDir: URL?,
        features: RoutingFeatures,
        preprocess: Bool,
        preprocessScript: String?,
        vlmLogs: inout [RunManifest.VLMRun],
        sessionStartedISO: inout String?,
        sessionEndedISO: inout String?,
        roiDurations: inout [Double],
        modelFingerprint: inout RunManifest.ModelFingerprint?
    ) throws {
        guard let exportDir else { return }

        var rois: [(id: String, pixel: PixelBox)] = []
        if let tables, !tables.isEmpty {
            for (index, table) in tables.enumerated() {
                let rect = CGRect(x: table.box.x, y: table.box.y, width: table.box.w, height: table.box.h)
                let pixel = pixelBox(from: rect, imageWidth: page.width, imageHeight: page.height)
                rois.append((id: "table_\(index)", pixel: pixel))
            }
        } else {
            let fullPixel = PixelBox(x: 0, y: 0, w: page.width, h: page.height)
            rois.append((id: "page", pixel: fullPixel))
        }

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted]

        // Persistent runner session (if using dots-ocrd.py)
        var persistent: (process: Process, inPipe: FileHandle, outPipe: FileHandle, errPipe: FileHandle)? = nil
        var sessionId: String? = nil
        if let runner = vlmRunner, runner.hasSuffix("dots-ocrd.py") {
            persistent = try startPersistentRunner(executable: runner)
            // Read hello
            if let hello = try readPersistentLine(from: persistent!.outPipe) {
                let runsDir = URL(fileURLWithPath: "runs/vlm", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
                try? FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
                let helloURL = runsDir.appendingPathComponent("runner_hello.json")
                try? hello.write(to: helloURL, atomically: true, encoding: .utf8)
                // Parse model fingerprint and mark session start
                if let data = hello.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let fp = obj["model_fingerprint"] as? [String: Any] {
                    modelFingerprint = RunManifest.ModelFingerprint(
                        name: fp["name"] as? String,
                        revision_sha: fp["revision_sha"] as? String,
                        quant: fp["quant"] as? String,
                        tokenizer_sha: fp["tokenizer_sha"] as? String,
                        chat_template_sha: fp["chat_template_sha"] as? String
                    )
                }
                let iso = ISO8601DateFormatter(); iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                if sessionStartedISO == nil { sessionStartedISO = iso.string(from: Date()) }
                // Deterministic session id: sess-YYYYMMDD-<docsha8>
                let dateFormatter = DateFormatter(); dateFormatter.dateFormat = "yyyyMMdd"; dateFormatter.timeZone = TimeZone(secondsFromGMT: 0)
                let day = dateFormatter.string(from: Date())
                let doc8 = String(docSHA.prefix(8))
                sessionId = "sess-\(day)-\(doc8)"
            }
        }

        for (idx, roi) in rois.enumerated() {
            let fileBase = "\(docSHA)_p\(payload.page_index)_roi\(idx)"
            var dataRequestSha: String? = nil
            var dataResponseSha: String? = nil
            let imageURL = exportDir.appendingPathComponent("\(fileBase).png")
            let requestURL = exportDir.appendingPathComponent("\(fileBase)_request.json")
            let responseURL = exportDir.appendingPathComponent("\(fileBase)_response.json")
            var headersReqURL = exportDir.appendingPathComponent("\(fileBase)_headers_request.json")
            var headersRespURL = exportDir.appendingPathComponent("\(fileBase)_headers_response.json")
            var dataReqURL = exportDir.appendingPathComponent("\(fileBase)_data_request.json")
            var dataRespURL = exportDir.appendingPathComponent("\(fileBase)_data_response.json")

            let cgRect = cgRect(from: roi.pixel, imageHeight: page.height)
            // Standardized ROI export: long-side=1792, pad=12
            try exportROI(image: page.image, rect: cgRect, longSide: 1792, padPx: 12, to: imageURL)

            // Record standard ROI export ops
            var preprocessOps: [String] = ["resize=1792long", "pad=12"]
            // Optional ROI pre-processing
            var usedImagePath = imageURL.path
            if preprocess {
                let preOut = exportDir.appendingPathComponent("\(fileBase)_pre.png")
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                let script = preprocessScript ?? "scripts/preprocess_roi.py"
                proc.arguments = ["python3", script, imageURL.path, preOut.path]
                let outPipe = Pipe()
                proc.standardOutput = outPipe
                proc.standardError = Pipe()
                do {
                    try proc.run()
                    proc.waitUntilExit()
                    if proc.terminationStatus == 0 {
                        usedImagePath = preOut.path
                        if let data = try? outPipe.fileHandleForReading.readToEnd(),
                           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                            let ops = json["ops"] as? [String] {
                            preprocessOps.append(contentsOf: ops)
                        }
                    }
                } catch {
                    // ignore preprocessor failure; fall back to original
                }
            }

            var hints: [String: Double] = [:]
            if let value = features.est_cols { hints["columns_est"] = Double(value) }
            if let value = features.numeric_density { hints["numeric_density"] = value }
            if let value = features.script_conflict_rate { hints["script_conflict_rate"] = value }
            if let value = features.paragraph_median_conf { hints["paragraph_median_conf"] = value }
            if let value = features.median_cell_conf { hints["median_cell_conf"] = value }
            hints["deskew_deg"] = 0
            hints["binarize"] = 0
            let est = estimatedCells(from: features)
            let bc = budgetClass(for: est.cells)
            hints["est_rows"] = Double(est.rows)
            hints["est_cols"] = Double(est.cols)
            hints["est_cells"] = Double(est.cells)
            hints["budget_class_idx"] = Double(bc.index)

            let requestPayload = VLMRequestEnvelope(
                schema: "vlm.request.v1",
                doc_sha256: docSHA,
                page_index: payload.page_index,
                roi_id: roi.id,
                image_path: usedImagePath,
                roi: VLMROI(px: roi.pixel.x, py: roi.pixel.y, pw: roi.pixel.w, ph: roi.pixel.h),
                hints: hints.isEmpty ? nil : hints
            )
            let requestData = try encoder.encode(requestPayload)
            try requestData.write(to: requestURL, options: .atomic)
            let requestCRC = crc32Hex(of: requestData)

            var responsePath: String? = nil
            var responseCRC: String? = nil
            var status = "skipped"
            if let runner = vlmRunner {
                do {
                    if runner.hasSuffix("dots-ocrd.py"), let session = persistent {
                        // Build infer message with base64 image bytes
                        let imageData = try Data(contentsOf: URL(fileURLWithPath: usedImagePath))
                        let b64 = imageData.base64EncodedString()
                        let infer: [String: Any] = [
                            "type": "infer",
                            "id": fileBase,
                            "image_b64": b64,
                            "roi_meta": ["w": roi.pixel.w, "h": roi.pixel.h, "scale": 1.0],
                            "decode": ["max_new_tokens": budgetFor(features: features), "timeout_s": 120, "greedy": true],
                            "schema_hint": NSNull(),
                            "payer_hint": NSNull(),
                            "compact": true,
                            "budget": [
                                "class": bc.name,
                                "est_rows": est.rows,
                                "est_cols": est.cols,
                                "est_cells": est.cells,
                                "est_source": "vision_lines"
                            ]
                        ]
                        // Emit infer payload for audit (non-breaking, aids CP4 validation)
                        do {
                            let inferData = try JSONSerialization.data(withJSONObject: infer, options: [.prettyPrinted])
                            try inferData.write(to: exportDir.appendingPathComponent("\(fileBase)_infer.json"))
                        } catch { /* best-effort */ }
                        _ = try JSONSerialization.data(withJSONObject: infer)
                        // debug log
                        do {
                            let runsDir = URL(fileURLWithPath: "runs/vlm", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
                            try? FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
                            let dbgURL = runsDir.appendingPathComponent("persistent_debug.log")
                            try? "infer \(fileBase)".appendLine(to: dbgURL)
                        }
                        // Build dual-pass header/data requests
                        // Compute image sha256 for header lock binding
                        let imgSHA = (try? sha256Digest(for: URL(fileURLWithPath: usedImagePath))) ?? String(docSHA.prefix(64))
                        let canCols = ["proc_cd","desc","dos","billed","allowed","paid","adjust","patient"]
                        let colTypes: [String: String] = ["proc_cd":"code","desc":"text","dos":"date","billed":"money","allowed":"money","paid":"money","adjust":"money","patient":"money"]
                        let roiId = "roi-" + String(docSHA.prefix(8)) + String(idx)
                        let hdrBudget: [String: Any] = ["class": bc.name, "est_rows": est.rows, "est_cols": est.cols, "est_cells": est.cells, "est_source": "vision_lines"]
                        let headersReq: [String: Any] = [
                            "type": "infer.headers.v1",
                            "session_id": sessionId ?? "sess-00000000-x",
                            "roi_id": roiId,
                            "image_sha256": imgSHA,
                            "image_path": usedImagePath,
                            "schema_hint": ["canonical_columns": canCols, "types": colTypes],
                            "payer_hint": NSNull(),
                            "decode": ["max_new_tokens": max(256, min(384, budgetFor(features: features))), "timeout_s": 60, "greedy": true],
                            "budget": hdrBudget,
                            "compact": true
                        ]
                        try JSONSerialization.data(withJSONObject: headersReq, options: [.prettyPrinted]).write(to: headersReqURL)
                        // Send headers pass
                        let t0 = DispatchTime.now()
                        let hdrWire = try JSONSerialization.data(withJSONObject: headersReq)
                        session.inPipe.write(hdrWire); session.inPipe.write(Data([0x0A]))
                        guard let hdrLine = try readPersistentLine(from: session.outPipe) else { throw CLIError.output("no header response") }
                        try hdrLine.write(to: headersRespURL, atomically: true, encoding: .utf8)
                        // Extract header_lock from response and retag artifacts with request hash
                        var headerLockObj: [String: Any] = [:]
                        if let d = hdrLine.data(using: .utf8), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                            if let hl = o["header_lock"] as? [String: Any] { headerLockObj = hl }
                            if let reqSha = o["request_sha256"] as? String {
                                let newReq = exportDir.appendingPathComponent("\(fileBase)_headers_request_\(reqSha).json")
                                headersReqURL = retagFile(original: headersReqURL, newURL: newReq)
                                let newResp = exportDir.appendingPathComponent("\(fileBase)_headers_response_\(reqSha).json")
                                headersRespURL = retagFile(original: headersRespURL, newURL: newResp)
                            }
                        }
                        // Build data pass request
                        let dataReq: [String: Any] = [
                            "type": "infer.data.v1",
                            "session_id": sessionId ?? "sess-00000000-x",
                            "roi_id": roiId,
                            "image_sha256": imgSHA,
                            "image_path": usedImagePath,
                            "header_lock": headerLockObj,
                            "decode": ["max_new_tokens": budgetFor(features: features), "timeout_s": 120, "greedy": true],
                            "budget": ["class": bc.name, "est_rows": est.rows, "est_cols": est.cols, "est_cells": est.cells, "est_source": "vision_lines"],
                            "compact": true
                        ]
                        try JSONSerialization.data(withJSONObject: dataReq, options: [.prettyPrinted]).write(to: dataReqURL)
                        // Send data pass
                        let dataWire = try JSONSerialization.data(withJSONObject: dataReq)
                        session.inPipe.write(dataWire); session.inPipe.write(Data([0x0A]))
                        if let dataLine = try readPersistentLine(from: session.outPipe) {
                            try dataLine.write(to: dataRespURL, atomically: true, encoding: .utf8)
                            responsePath = dataRespURL.path
                            if let d = dataLine.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                                responseCRC = crc32Hex(of: d)
                                if let reqSha = obj["request_sha256"] as? String {
                                    dataRequestSha = reqSha
                                    let newReq = exportDir.appendingPathComponent("\(fileBase)_data_request_\(reqSha).json")
                                    dataReqURL = retagFile(original: dataReqURL, newURL: newReq)
                                    let newResp = exportDir.appendingPathComponent("\(fileBase)_data_response_\(reqSha).json")
                                    dataRespURL = retagFile(original: dataRespURL, newURL: newResp)
                                    responsePath = dataRespURL.path
                                }
                                if let respSha = obj["response_sha256"] as? String {
                                    dataResponseSha = respSha
                                }
                            }
                        }
                        let t1 = DispatchTime.now(); let dt = Double(t1.uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000_000.0; roiDurations.append(dt)
                        status = "ok"
                    } else {
                        let responseData = try invokeVLMRunner(executable: runner, requestData: requestData)
                        if let data = responseData {
                            try validateVLMResponse(data: data)
                            try data.write(to: responseURL, options: .atomic)
                            responsePath = responseURL.path
                            responseCRC = crc32Hex(of: data)
                        }
                        status = "ok"
                    }
                } catch {
                    status = "error: \(error)"
                }
            }

            vlmLogs.append(
                RunManifest.VLMRun(
                    page_index: payload.page_index,
                    roi_id: roi.id,
                    image_path: usedImagePath,
                    request_path: requestURL.path,
                    response_path: responsePath,
                    request_crc32: requestCRC,
                    response_crc32: responseCRC,
                    runner_status: status,
                    preprocess_ops: preprocessOps.isEmpty ? nil : preprocessOps,
                    request_sha256: dataRequestSha,
                    response_sha256: dataResponseSha
                )
            )
        }

        // Close persistent runner session
        if let session = persistent {
            let closeMsg = "{\"type\":\"close\"}\n".data(using: .utf8)!
            session.inPipe.write(closeMsg)
            let maybeLine = try? readPersistentLine(from: session.outPipe)
            if let s = maybeLine {
                let iso = ISO8601DateFormatter(); iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                sessionEndedISO = iso.string(from: Date())
                let runsDir = URL(fileURLWithPath: "runs/vlm", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
                let byeURL = runsDir.appendingPathComponent("runner_goodbye.json")
                try? s.write(to: byeURL, atomically: true, encoding: .utf8)
            }
            session.inPipe.closeFile()
            session.outPipe.closeFile()
            // capture stderr to a log file for diagnostics
            let errData = (try? session.errPipe.readToEnd()) ?? Data()
            if !errData.isEmpty {
                let runsDir = URL(fileURLWithPath: "runs/vlm", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
                try? FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
                let logURL = runsDir.appendingPathComponent("runner_stderr.log")
                if let s = String(data: errData, encoding: .utf8) {
                    try? (s + "\n").appendLine(to: logURL)
                }
            }
            session.errPipe.closeFile()
            session.process.terminate()
        }
    }

    private static func computeSessionStats(startISO: String?, endISO: String?, durations: [Double]) -> RunManifest.SessionStats? {
        if startISO == nil && endISO == nil && durations.isEmpty { return nil }
        let avg = durations.isEmpty ? nil : (durations.reduce(0,+) / Double(durations.count))
        let p95: Double? = {
            guard !durations.isEmpty else { return nil }
            let sorted = durations.sorted()
            let idx = max(0, min(sorted.count-1, Int(ceil(0.95 * Double(sorted.count))) - 1))
            return sorted[idx]
        }()
        return RunManifest.SessionStats(started_ts: startISO, ended_ts: endISO, roi_count: durations.isEmpty ? nil : durations.count, avg_sec_per_roi: avg, p95_sec_per_roi: p95)
    }

    private static func budgetFor(features: RoutingFeatures) -> Int {
        let estCols = features.est_cols ?? 6
        let estRows = 8
        let estCells = estCols * estRows
        let base = 384
        let perCell = 10
        let cap = 2048
        let tokens = base + perCell * min(estCells, 120)
        return max(256, min(tokens, cap))
    }

    private static func estimatedCells(from features: RoutingFeatures) -> (rows: Int, cols: Int, cells: Int) {
        let cols = max(1, features.est_cols ?? 6)
        let rows = 8 // fallback default; future: compute from ROI projections
        let cells = max(1, rows * cols)
        return (rows, cols, cells)
    }

    private static func budgetClass(for estCells: Int) -> (index: Int, name: String) {
        if estCells < 36 { return (0, "small") }
        if estCells <= 120 { return (1, "typical") }
        return (2, "large")
    }

    private static func startPersistentRunner(executable: String) throws -> (Process, FileHandle, FileHandle, FileHandle) {
        let p = Process()
        let env = ProcessInfo.processInfo.environment
        if let py = env["VLM_PYTHON"], !py.isEmpty {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = [py, "-u", executable]
        } else {
            // Shebang fallback
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", "-u", executable]
        }
        p.currentDirectoryURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        var penv = ProcessInfo.processInfo.environment
        penv["PYTHONUNBUFFERED"] = "1"
        p.environment = penv
        let inPipe = Pipe()
        let outPipe = Pipe()
        let errPipe = Pipe()
        p.standardInput = inPipe
        p.standardOutput = outPipe
        p.standardError = errPipe
        try p.run()
        return (p, inPipe.fileHandleForWriting, outPipe.fileHandleForReading, errPipe.fileHandleForReading)
    }

    private static func readPersistentLine(from fh: FileHandle) throws -> String? {
        var buffer = Data()
        while true {
            let chunk = try fh.read(upToCount: 1) ?? Data()
            if chunk.isEmpty { break }
            if chunk[0] == 0x0A { break }
            buffer.append(chunk)
        }
        if buffer.isEmpty { return nil }
        return String(data: buffer, encoding: .utf8)
    }

    private static func cgRect(from pixel: PixelBox, imageHeight: Int) -> CGRect {
        let x = CGFloat(pixel.x)
        let y = CGFloat(max(0, imageHeight - pixel.y - pixel.h))
        let width = CGFloat(pixel.w)
        let height = CGFloat(pixel.h)
        return CGRect(x: x, y: y, width: width, height: height)
    }

    private static func makeROIImage(image: CGImage, pixel: PixelBox, scale: CGFloat) throws -> CGImage? {
        let rect = cgRect(from: pixel, imageHeight: image.height)
        guard rect.width > 0, rect.height > 0 else { return nil }
        guard let cropped = image.cropping(to: rect.integral) else { return nil }

        if scale <= 1.0 {
            return cropped
        }

        let width = max(1, Int(CGFloat(cropped.width) * scale))
        let height = max(1, Int(CGFloat(cropped.height) * scale))
        let cs = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw CLIError.output("roi scale ctx")
        }
        ctx.interpolationQuality = .high
        ctx.draw(cropped, in: CGRect(x: 0, y: 0, width: width, height: height))
        return ctx.makeImage()
    }

    private static func exportROI(image: CGImage, rect: CGRect, longSide: CGFloat, padPx: Int, to url: URL) throws {
        let cropRect = CGRect(
            x: max(0, rect.origin.x),
            y: max(0, rect.origin.y),
            width: min(CGFloat(image.width) - rect.origin.x, rect.width),
            height: min(CGFloat(image.height) - rect.origin.y, rect.height)
        )
        guard cropRect.width > 0, cropRect.height > 0 else { throw CLIError.output("invalid ROI") }
        guard let cropped = image.cropping(to: cropRect.integral) else { throw CLIError.output("roi crop failed") }

        // Target content long side accounts for padding on both sides.
        let targetContentLong = max(1, Int(longSide) - (2 * max(0, padPx)))
        let maxSide = max(CGFloat(cropped.width), CGFloat(cropped.height))
        let scale = maxSide > 0 ? min(1.0, CGFloat(targetContentLong) / maxSide) : 1.0

        // Resize content if needed
        let resized: CGImage
        if scale < 1.0 {
            let width = max(1, Int(CGFloat(cropped.width) * scale))
            let height = max(1, Int(CGFloat(cropped.height) * scale))
            let cs = CGColorSpaceCreateDeviceRGB()
            guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
                throw CLIError.output("resize ctx")
            }
            ctx.interpolationQuality = .high
            ctx.draw(cropped, in: CGRect(x: 0, y: 0, width: width, height: height))
            guard let scaled = ctx.makeImage() else { throw CLIError.output("resize fail") }
            resized = scaled
        } else {
            resized = cropped
        }

        // Pad with white background to achieve final longSide target
        let contentW = resized.width
        let contentH = resized.height
        let outW: Int
        let outH: Int
        if contentW >= contentH {
            outW = max(contentW + 2 * padPx, Int(longSide))
            outH = contentH + 2 * padPx
        } else {
            outH = max(contentH + 2 * padPx, Int(longSide))
            outW = contentW + 2 * padPx
        }
        let cs2 = CGColorSpaceCreateDeviceRGB()
        guard let padCtx = CGContext(data: nil, width: outW, height: outH, bitsPerComponent: 8, bytesPerRow: 0, space: cs2, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw CLIError.output("pad ctx")
        }
        padCtx.interpolationQuality = .high
        padCtx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        padCtx.fill(CGRect(x: 0, y: 0, width: outW, height: outH))
        padCtx.draw(resized, in: CGRect(x: padPx, y: padPx, width: contentW, height: contentH))
        guard let finalImage = padCtx.makeImage() else { throw CLIError.output("pad finalize fail") }

        guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw CLIError.output("roi dest")
        }
        CGImageDestinationAddImage(dest, finalImage, nil)
        if !CGImageDestinationFinalize(dest) {
            throw CLIError.output("roi finalize")
        }
    }

    private static func invokeVLMRunner(executable: String, requestData: Data) throws -> Data? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        let inputPipe = Pipe()
        let outputPipe = Pipe()
        process.standardInput = inputPipe
        process.standardOutput = outputPipe
        process.standardError = Pipe()
        try process.run()
        inputPipe.fileHandleForWriting.write(requestData)
        inputPipe.fileHandleForWriting.closeFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw CLIError.output("vlm runner exit status \(process.terminationStatus)")
        }
        return try outputPipe.fileHandleForReading.readToEnd()
    }

    private static func validateVLMResponse(data: Data) throws {
        let object = try JSONSerialization.jsonObject(with: data)
        guard let dict = object as? [String: Any], let schema = dict["schema"] as? String, schema == "vlm.response.v1" else {
            throw CLIError.output("invalid VLM response schema")
        }
    }

    private static func crc32Hex(of data: Data) -> String {
        return data.withUnsafeBytes { buffer -> String in
            guard let base = buffer.bindMemory(to: UInt8.self).baseAddress else { return "00000000" }
            let value = crc32(0, base, uInt(buffer.count))
            return String(format: "%08x", value)
        }
    }

    private static func makeLayoutText(from lines: [Line], columns: Int = 120) -> String? {
        guard !lines.isEmpty else { return nil }
        struct Row {
            var top: Double
            var entries: [(index: Int, line: Line)]
        }
        let sorted = lines.enumerated().sorted { lhs, rhs in
            let lhsTop = Double(lhs.element.box.y + lhs.element.box.h)
            let rhsTop = Double(rhs.element.box.y + rhs.element.box.h)
            if abs(lhsTop - rhsTop) > 0.015 {
                return lhsTop > rhsTop
            }
            return lhs.element.box.x < rhs.element.box.x
        }
        var rows: [Row] = []
        for entry in sorted {
            let top = Double(entry.element.box.y + entry.element.box.h)
            if let last = rows.last, abs(last.top - top) <= 0.015 {
                rows[rows.count - 1].entries.append((entry.offset, entry.element))
            } else {
                rows.append(Row(top: top, entries: [(entry.offset, entry.element)]))
            }
        }
        var layoutLines: [String] = []
        for row in rows {
            var buffer = Array(repeating: Character(" "), count: columns)
            let rowEntries = row.entries.sorted { lhs, rhs in
                let lhsX = Double(lhs.line.box.x)
                let rhsX = Double(rhs.line.box.x)
                if abs(lhsX - rhsX) > 0.001 {
                    return lhsX < rhsX
                }
                return lhs.index < rhs.index
            }
            for entry in rowEntries {
                let start = max(0, min(columns - 1, Int(Double(columns) * Double(entry.line.box.x))))
                var col = start
                for character in entry.line.text {
                    if col >= columns { break }
                    buffer[col] = character
                    col += 1
                }
            }
            let rowString = String(buffer).trimmedTrailingSpaces()
            layoutLines.append(rowString)
        }
        while let last = layoutLines.last, last.trimmingCharacters(in: .whitespaces).isEmpty {
            layoutLines.removeLast()
        }
        guard !layoutLines.isEmpty else { return nil }
        return layoutLines.joined(separator: "\n")
    }

    private static func computeRegions(lines: [Line], pageWidth: Int, pageHeight: Int) -> PageRegions? {
        guard !lines.isEmpty else { return nil }
        let topThreshold = 0.82
        var leftIndices: [Int] = []
        var rightIndices: [Int] = []
        for (index, line) in lines.enumerated() {
            let top = Double(line.box.y + line.box.h)
            if top < topThreshold { continue }
            let centerX = Double(line.box.x + (line.box.w / 2.0))
            if centerX < 0.5 {
                leftIndices.append(index)
            } else {
                rightIndices.append(index)
            }
        }
        let leftRegion = makeRegion(indices: leftIndices, lines: lines, pageWidth: pageWidth, pageHeight: pageHeight)
        let rightRegion = makeRegion(indices: rightIndices, lines: lines, pageWidth: pageWidth, pageHeight: pageHeight)
        if leftRegion == nil && rightRegion == nil {
            return nil
        }
        return PageRegions(header: HeaderRegions(left: leftRegion, right: rightRegion))
    }

    private static func makeRegion(indices: [Int], lines: [Line], pageWidth: Int, pageHeight: Int) -> Region? {
        guard !indices.isEmpty else { return nil }
        let sorted = indices.sorted { lhs, rhs in
            let lhsTop = Double(lines[lhs].box.y + lines[lhs].box.h)
            let rhsTop = Double(lines[rhs].box.y + lines[rhs].box.h)
            if abs(lhsTop - rhsTop) > 0.015 {
                return lhsTop > rhsTop
            }
            return lines[lhs].box.x < lines[rhs].box.x
        }
        var minX = Double.greatestFiniteMagnitude
        var minY = Double.greatestFiniteMagnitude
        var maxX = -Double.greatestFiniteMagnitude
        var maxY = -Double.greatestFiniteMagnitude
        var minPx = Int.max
        var minPy = Int.max
        var maxPx = Int.min
        var maxPy = Int.min
        var textLines: [String] = []
        for index in sorted {
            let line = lines[index]
            minX = min(minX, Double(line.box.x))
            minY = min(minY, Double(line.box.y))
            maxX = max(maxX, Double(line.box.x + line.box.w))
            maxY = max(maxY, Double(line.box.y + line.box.h))
            let pixel = line.pixel_box
            minPx = min(minPx, pixel.x)
            minPy = min(minPy, pixel.y)
            maxPx = max(maxPx, pixel.x + pixel.w)
            maxPy = max(maxPy, pixel.y + pixel.h)
            textLines.append(line.text)
        }
        guard minX.isFinite, minY.isFinite, maxX.isFinite, maxY.isFinite else { return nil }
        let clampedMinX = clamp01(minX)
        let clampedMinY = clamp01(minY)
        let clampedMaxX = clamp01(maxX)
        let clampedMaxY = clamp01(maxY)
        let normalized = Box(
            x: CGFloat(clampedMinX),
            y: CGFloat(clampedMinY),
            w: CGFloat(max(0.0, clampedMaxX - clampedMinX)),
            h: CGFloat(max(0.0, clampedMaxY - clampedMinY))
        )
        let clampedMinPx = max(0, min(minPx, pageWidth))
        let clampedMinPy = max(0, min(minPy, pageHeight))
        let clampedMaxPx = max(clampedMinPx + 1, min(maxPx, pageWidth))
        let clampedMaxPy = max(clampedMinPy + 1, min(maxPy, pageHeight))
        let pixel = PixelBox(
            x: clampedMinPx,
            y: clampedMinPy,
            w: max(1, clampedMaxPx - clampedMinPx),
            h: max(1, clampedMaxPy - clampedMinPy)
        )
        let regionBox = RegionBox(normalized: normalized, pixel: pixel)
        let text = textLines.joined(separator: "\n")
        return Region(box: regionBox, line_indices: sorted, text: text)
    }

    private static func writeRasterSidecar(docSHA: String, pageIndex: Int, width: Int, height: Int, dpi: Int) {
        let runsDir = URL(fileURLWithPath: "runs", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
        let outURL = runsDir.appendingPathComponent("\(docSHA)_p\(pageIndex)_raster.json")
        let payload: [String: Any] = [
            "doc_sha256": docSHA,
            "page_index": pageIndex,
            "raster": [
                "dpi": dpi,
                "colorspace": "RGB",
                "interpolation": "high",
                "cropbox": [0, 0, width, height]
            ]
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [])
            try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
            try data.write(to: outURL)
        } catch {
            // best-effort; do not fail page processing
        }
    }

    private static func writePreprocSidecar(
        docSHA: String,
        pageIndex: Int,
        width: Int,
        height: Int,
        dpi: Int,
        pageConfMedian: Double?,
        headerConfMedian: Double?,
        decision: Route,
        features: RoutingFeatures,
        roiClassifications: [[String: Any]],
        routeReason: String
    ) {
        let runsDir = URL(fileURLWithPath: "runs", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
        let outURL = runsDir.appendingPathComponent("preproc_\(docSHA)_p\(pageIndex).json")
        let est = estimatedCells(from: features)
        var med: [String: Any] = [:]
        if let m = pageConfMedian { med["page_conf_median"] = m }
        if let h = headerConfMedian { med["header_conf_median"] = h }
        let action: String = (decision == .vlm ? "route_vlm" : (decision == .text ? "accept_vision" : "accept_vision"))
        let payload: [String: Any] = [
            "pdf_sha256": docSHA,
            "page_index": pageIndex,
            "raster": [
                "dpi": dpi,
                "colorspace": "RGB",
                "interpolation": "cubic",
                "cropbox": ["x": 0, "y": 0, "width": width, "height": height]
            ],
            "preprocess_ops": [
                "applied": ["clahe", "deskew=0.0", "pad=12", "resize=1792long"],
                "deskew_deg": 0.0,
                "clahe": ["clip_limit": 3.0, "tile_grid": [8, 8]],
                "pad_px": 12,
                "resize_applied": true,
                "long_side_target": 1792
            ],
            "medians": med,
            "route_decision": [
                "action": action,
                "reason": routeReason,
                "retry_count": 0
            ],
            "vision_estimate": [
                "est_rows": est.rows,
                "est_cols": est.cols,
                "est_cells": est.cells,
                "est_source": "vision_lines"
            ],
            "roi_classifications": roiClassifications
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [])
            try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
            try data.write(to: outURL)
        } catch {
            // best-effort only
        }
    }

    private static func computeRouteReason(decision: Route, metrics: PageMetrics, tau: Double) -> String {
        if decision == .vlm {
            if let maxScript = metrics.scriptConflictRateMax, maxScript > 0.05 {
                return "vlm:script_conflict_max>0.05"
            }
            if (metrics.columnCount >= 5) {
                if let mcc = metrics.medianCellConfidence, mcc < 0.70 {
                    return "vlm:table_low_cell_conf<0.70"
                }
                if let nd = metrics.numericDensity, nd >= 0.55 {
                    return "vlm:table_high_numeric_density>=0.55"
                }
            }
            let par = metrics.paragraphMedianConfidence ?? (metrics.meanConfidence ?? 0.0)
            if par < tau {
                return "vlm:low_paragraph_conf<tau"
            }
            return "vlm:heuristic_trigger"
        }
        if decision == .skip {
            return "skip:non_content"
        }
        return "vision:acceptable"
    }

    private static func writePreprocFeaturesSHA(docSHA: String, pageIndex: Int, roiClassifications: [[String: Any]]) {
        // Build a canonical string from roi classifications (sorted by roi_id; fixed feature order)
        let runsDir = URL(fileURLWithPath: "runs", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
        let outURL = runsDir.appendingPathComponent("preproc_\(docSHA)_p\(pageIndex).features.sha256")
        do {
            let sorted = roiClassifications.sorted { (a, b) -> Bool in
                let ra = (a["roi_id"] as? String) ?? ""
                let rb = (b["roi_id"] as? String) ?? ""
                return ra < rb
            }
            var lines: [String] = []
            for item in sorted {
                let roi = (item["roi_id"] as? String) ?? ""
                let features = (item["features"] as? [String: Any]) ?? [:]
                let H_pos = (features["H_pos"] as? Int) ?? 0
                let H_lex = (features["H_lex"] as? Int) ?? 0
                let M_money = (features["M_money"] as? Int) ?? 0
                let M_codes = (features["M_codes"] as? Int) ?? 0
                let N_foot = (features["N_foot"] as? Int) ?? 0
                let cy = (features["cy"] as? Double) ?? 0.0
                let line = "roi_id=\(roi);H_pos=\(H_pos);H_lex=\(H_lex);M_money=\(M_money);M_codes=\(M_codes);N_foot=\(N_foot);cy=\(String(format: "%.6f", cy))"
                lines.append(line)
            }
            let canonical = lines.joined(separator: "\n")
            // SHA256
            if let data = canonical.data(using: .utf8) {
                let digest = SHA256.hash(data: data)
                let hex = digest.compactMap { String(format: "%02x", $0) }.joined()
                try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
                try (hex + "\n").data(using: .utf8)?.write(to: outURL)
            }
        } catch {
            // best-effort only
        }
    }

    private static func pixelBox(from rect: CGRect, imageWidth: Int, imageHeight: Int) -> PixelBox {
        let rawX = Int(round(rect.origin.x * CGFloat(imageWidth)))
        let rawY = Int(round(rect.origin.y * CGFloat(imageHeight)))
        let clampedX = max(0, min(rawX, max(0, imageWidth - 1)))
        let clampedY = max(0, min(rawY, max(0, imageHeight - 1)))
        let rawW = Int(round(rect.size.width * CGFloat(imageWidth)))
        let rawH = Int(round(rect.size.height * CGFloat(imageHeight)))
        let maxWidth = max(1, imageWidth - clampedX)
        let maxHeight = max(1, imageHeight - clampedY)
        let clampedW = max(1, min(rawW, maxWidth))
        let clampedH = max(1, min(rawH, maxHeight))
        return PixelBox(x: clampedX, y: clampedY, w: clampedW, h: clampedH)
    }

    private static func clamp01(_ value: Double) -> Double {
        return min(1.0, max(0.0, value))
    }

    private static func makeROIClassifications(
        regions: PageRegions?,
        tables: [Table]?,
        lines: [Line],
        pageWidth: Int,
        pageHeight: Int
    ) -> [[String: Any]] {
        // Helper regexes
        let moneyRegex = try! NSRegularExpression(pattern: #"\$?\b\d{1,3}(?:[,\']\d{3})*(?:\.\d{2})\b"#, options: [])
        let codeRegex = try! NSRegularExpression(pattern: #"\bD\d{4}[A-Z]?\b"#, options: [.caseInsensitive])
        let headerKeywords: Set<String> = ["EOB","EXPLANATION","PROVIDER","SUBSCRIBER","MEMBER","CLAIM","CHECK","ERA"]

        func countMatches(_ rx: NSRegularExpression, _ text: String) -> Int {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            return rx.numberOfMatches(in: text, options: [], range: range)
        }
        func headerLex(_ text: String) -> Int {
            let tokens = text.uppercased().split{ !$0.isLetter && !$0.isNumber }.map(String.init)
            return tokens.filter { headerKeywords.contains($0) }.count
        }
        func classify(H_pos: Int, H_lex: Int, M_money: Int, M_codes: Int, N_foot: Int) -> String {
            if (H_pos == 1 && H_lex >= 1) || (H_lex >= 2) { return "header" }
            if (M_money >= 6) || (M_money >= 3 && M_codes >= 2) { return "money" }
            if N_foot == 1 { return "notes" }
            return "money"
        }

        var out: [[String: Any]] = []
        // Header regions (left/right)
        if let hdr = regions?.header {
            if let left = hdr.left {
                let cy = Double(left.box.pixel.y + left.box.pixel.h/2) / Double(pageHeight)
                let H_pos = (cy <= 0.25) ? 1 : 0
                let N_foot = (cy >= 0.75) ? 1 : 0
                let text = left.text
                let H_lex = headerLex(text)
                let M_money = countMatches(moneyRegex, text)
                let M_codes = countMatches(codeRegex, text)
                let cls = classify(H_pos: H_pos, H_lex: H_lex, M_money: M_money, M_codes: M_codes, N_foot: N_foot)
                out.append([
                    "roi_id": "header_left",
                    "classification": cls,
                    "features": ["H_pos": H_pos, "H_lex": H_lex, "M_money": M_money, "M_codes": M_codes, "N_foot": N_foot, "cy": cy]
                ])
            }
            if let right = hdr.right {
                let cy = Double(right.box.pixel.y + right.box.pixel.h/2) / Double(pageHeight)
                let H_pos = (cy <= 0.25) ? 1 : 0
                let N_foot = (cy >= 0.75) ? 1 : 0
                let text = right.text
                let H_lex = headerLex(text)
                let M_money = countMatches(moneyRegex, text)
                let M_codes = countMatches(codeRegex, text)
                let cls = classify(H_pos: H_pos, H_lex: H_lex, M_money: M_money, M_codes: M_codes, N_foot: N_foot)
                out.append([
                    "roi_id": "header_right",
                    "classification": cls,
                    "features": ["H_pos": H_pos, "H_lex": H_lex, "M_money": M_money, "M_codes": M_codes, "N_foot": N_foot, "cy": cy]
                ])
            }
        }
        // Table ROIs
        if let tables = tables {
            for (i, t) in tables.enumerated() {
                let cy = Double(t.box.y + t.box.h/2)
                let H_pos = (cy <= 0.25) ? 1 : 0
                let N_foot = (cy >= 0.75) ? 1 : 0
                let allText = t.cells.map { $0.text }.joined(separator: "\n")
                let H_lex = headerLex(allText)
                let M_money = countMatches(moneyRegex, allText)
                let M_codes = countMatches(codeRegex, allText)
                let cls = classify(H_pos: H_pos, H_lex: H_lex, M_money: M_money, M_codes: M_codes, N_foot: N_foot)
                out.append([
                    "roi_id": "table_\(i)",
                    "classification": cls,
                    "features": ["H_pos": H_pos, "H_lex": H_lex, "M_money": M_money, "M_codes": M_codes, "N_foot": N_foot, "cy": cy]
                ])
            }
        }
        return out
    }

    private static func prepareOutputHandle(at url: URL?) throws -> FileHandle {
        if let url = url {
            let directory = url.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: nil)
            FileManager.default.createFile(atPath: url.path, contents: nil)
            return try FileHandle(forWritingTo: url)
        }
        return FileHandle.standardOutput
    }

    private static func loadImages(from url: URL, dpi: Int, slice: CLIOptions.PageSlice?) throws -> [PageImage] {
        var results: [PageImage] = []
        let ext = url.pathExtension.lowercased()
        if ext == "pdf" {
            guard let document = CGPDFDocument(url as CFURL) else {
                throw CLIError.load("failed to open PDF: \(url.path)")
            }
            let totalPages = document.numberOfPages
            let range = try slice?.range(clampingTo: totalPages) ?? (0..<totalPages)
            for logicalIndex in range {
                guard let page = document.page(at: logicalIndex + 1) else {
                    continue
                }
                let rendered = try render(pdfPage: page, dpi: CGFloat(dpi))
                results.append(PageImage(index: logicalIndex, image: rendered.image, width: rendered.width, height: rendered.height))
            }
        } else {
            if let slice {
                let range = try slice.range(clampingTo: 1)
                guard range.contains(0) else {
                    throw CLIError.usage("--pages slice excludes the only page in raster image")
                }
            }
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
                throw CLIError.load("failed to load raster image: \(url.path)")
            }
            results.append(PageImage(index: 0, image: image, width: image.width, height: image.height))
        }
        return results
    }

    private static func render(pdfPage: CGPDFPage, dpi: CGFloat) throws -> (image: CGImage, width: Int, height: Int) {
        let mediaBox = pdfPage.getBoxRect(.mediaBox)
        let scale = dpi / 72.0
        let width = max(1, Int(mediaBox.width * scale))
        let height = max(1, Int(mediaBox.height * scale))
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            throw CLIError.load("failed to create CGContext for PDF rasterization")
        }
        context.interpolationQuality = .high
        context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        // Use CoreGraphics-provided drawing transform to respect page rotation and aspect ratio.
        let destRect = CGRect(x: 0, y: 0, width: CGFloat(width), height: CGFloat(height))
        let transform = pdfPage.getDrawingTransform(.mediaBox, rect: destRect, rotate: 0, preserveAspectRatio: true)
        context.saveGState()
        context.concatenate(transform)
        context.drawPDFPage(pdfPage)
        context.restoreGState()
        guard let image = context.makeImage() else {
            throw CLIError.load("failed to rasterize PDF page")
        }
        return (image, width, height)
    }

    private static func crc32Hex(for image: CGImage) -> String {
        guard let provider = image.dataProvider, let cfData = provider.data else {
            return String(format: "%08x", 0)
        }
        let length = CFDataGetLength(cfData)
        guard let bytes = CFDataGetBytePtr(cfData) else {
            return String(format: "%08x", 0)
        }
        let crc = crc32(0, bytes, uInt(length))
        return String(format: "%08x", crc)
    }

    private static func sha256Digest(for url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while autoreleasepool(invoking: {
            let data = try? handle.read(upToCount: 1 << 20)
            if let data = data, !data.isEmpty {
                hasher.update(data: data)
                return true
            }
            return false
        }) {}
        let digest = hasher.finalize()
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private static func resolveLogURL(preferred: URL?, schemaVersion: String) throws -> URL {
        if let preferred = preferred {
            let directory = preferred.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: nil)
            return preferred
        }
        let runsDir = URL(fileURLWithPath: "runs", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)).standardizedFileURL
        try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true, attributes: nil)
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let timestamp = formatter.string(from: Date())
        let filename = "\(timestamp)_run_v\(schemaVersion).json"
        return runsDir.appendingPathComponent(filename)
    }

    private static func makeRunManifest(
        schemaVersion: String,
        options: CLIOptions,
        pageLogs: [RunManifest.PageLog],
        totalDuration: Int,
        sha256: String,
        engineInfo: EngineInfo,
        logPath: URL,
        routingStats: RoutingStats,
        processingMode: CLIOptions.Mode,
        modeFallback: Bool,
        vlmRuns: [RunManifest.VLMRun],
        ndjsonCRC32: String,
        ndjsonLineCount: Int,
        runnerMode: String?,
        modelFingerprint: RunManifest.ModelFingerprint?,
        cachePaths: RunManifest.CachePaths?,
        sessionStats: RunManifest.SessionStats?
    ) throws -> RunManifest {
        let os = ProcessInfo.processInfo.operatingSystemVersion
        let macOSVersion = "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"
        let swiftVersion = captureCommand(["swift", "--version"])
        let xcodeVersion = captureCommand(["xcodebuild", "-version"])
        let gitSHA = captureCommand(["git", "rev-parse", "HEAD"])
        let ndjsonPath = options.outputURL?.path ?? "<stdout>"
        let isoFormatter = ISO8601DateFormatter()
        isoFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let routingInfo = RunManifest.RoutingManifest(
            version: routingVersion,
            tau_conf: routingStats.lastTau,
            median_confidence: routingStats.medianConfidence(),
            features: [
                "tau_conf": true,
                "blank_guard": true,
                "columns_gate": true,
                "script_conflict": true,
                "retry_lc": false
            ]
        )

        let manifest = RunManifest(
            timestamp: isoFormatter.string(from: Date()),
            argv: CommandLine.arguments,
            macOS: macOSVersion,
            swift_version: swiftVersion,
            xcode_version: xcodeVersion,
            cli_git_sha: gitSHA,
            input_path: options.inputPath,
            input_sha256: sha256,
            ndjson_path: ndjsonPath,
            ndjson_crc32: ndjsonCRC32,
            ndjson_line_count: ndjsonLineCount,
            schema_version: schemaVersion,
            dpi: options.dpi,
            mode: processingMode.rawValue,
            mode_fallback: modeFallback,
            lang: options.languages,
            timeout_ms: options.timeoutMS,
            page_count: pageLogs.count,
            duration_ms: totalDuration,
            engine: engineInfo.engine,
            revision: engineInfo.revision,
            min_text_height: Double(minimumTextHeight),
            routing: routingInfo,
            vlm_runs: vlmRuns.isEmpty ? nil : vlmRuns,
            pages: pageLogs,
            runner_mode: runnerMode,
            model_fingerprint: modelFingerprint,
            cache_paths: cachePaths,
            session: sessionStats
        )

        return manifest
    }

    private static func captureCommand(_ command: [String]) -> String? {
        guard !command.isEmpty else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = command
        process.currentDirectoryURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8) else {
            return nil
        }
        return output.split(separator: "\n").first.map(String.init)
    }

    private static func renderDebugOverlay(
        for page: PageImage,
        lines: [Line],
        paragraphs: [Paragraph]?,
        tables: [Table]?,
        to url: URL
    ) throws {
        let width = page.width
        let height = page.height
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            throw CLIError.output("failed to create debug overlay context")
        }
        context.draw(page.image, in: CGRect(x: 0, y: 0, width: width, height: height))
        context.setLineWidth(2.0)

        context.setStrokeColor(CGColor(red: 1, green: 0, blue: 0, alpha: 0.8))
        for line in lines {
            context.stroke(rectFor(box: line.box, width: width, height: height))
        }

        if let paragraphs = paragraphs {
            context.setStrokeColor(CGColor(red: 0, green: 0, blue: 1, alpha: 0.7))
            for paragraph in paragraphs {
                context.stroke(rectFor(box: paragraph.box, width: width, height: height))
            }
        }

        if let tables = tables {
            context.setStrokeColor(CGColor(red: 0, green: 0.6, blue: 0, alpha: 0.7))
            for table in tables {
                context.stroke(rectFor(box: table.box, width: width, height: height))
            }
        }

        guard let overlayImage = context.makeImage() else {
            throw CLIError.output("failed to materialize debug overlay")
        }
        guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw CLIError.output("failed to open overlay destination")
        }
        CGImageDestinationAddImage(destination, overlayImage, nil)
        if !CGImageDestinationFinalize(destination) {
            throw CLIError.output("failed to finalize overlay")
        }
    }

    private static func rectFor(box: Box, width: Int, height: Int) -> CGRect {
        let rectWidth = CGFloat(width) * box.w
        let rectHeight = CGFloat(height) * box.h
        let originX = CGFloat(width) * box.x
        let originY = CGFloat(height) * box.y
        return CGRect(x: originX, y: originY, width: rectWidth, height: rectHeight)
    }
}
private func normalizeLanguageCode(_ code: String) -> String? {
    let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
    let lowered = trimmed.lowercased()
    if lowered == "c" || lowered == "c.utf-8" || lowered == "posix" {
        return "en-US"
    }
    if trimmed.isEmpty {
        return nil
    }
    return trimmed
}

private extension String {
    func trimmedTrailingSpaces() -> String {
        guard let lastNonSpace = lastIndex(where: { $0 != " " }) else { return "" }
        return String(self[...lastNonSpace])
    }
}

private extension String {
    func appendLine(to url: URL) throws {
        let data = (self + "\n").data(using: .utf8) ?? Data()
        if FileManager.default.fileExists(atPath: url.path) {
            let handle = try FileHandle(forWritingTo: url)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
        } else {
            try data.write(to: url)
        }
    }
}
