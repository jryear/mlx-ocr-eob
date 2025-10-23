import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
let outputPath = arguments.count > 1 ? arguments[1] : "fixtures/sample.pdf"
let url = URL(fileURLWithPath: outputPath)
let fm = FileManager.default
try? fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)

let pageRect = CGRect(x: 0, y: 0, width: 612, height: 792)
var mediaBox = pageRect

guard let consumer = CGDataConsumer(url: url as CFURL) else {
    fatalError("Unable to create data consumer")
}

guard let context = CGContext(consumer: consumer, mediaBox: &mediaBox, nil) else {
    fatalError("Unable to create PDF context")
}

context.beginPDFPage(nil)
context.setFillColor(gray: 0, alpha: 1)
context.selectFont("Helvetica", size: 18, matrix: .identity)
context.setTextDrawingMode(.fill)
context.setTextMatrix(.identity)

let lines = [
    "NativeOCR Sample Statement",
    "Account: 123456",
    "Member: Jane Doe",
    "Service Date | Code | Amount",
    "2024-07-01   | A123 | $120.00",
    "2024-07-08   | B456 | $85.50",
    "",
    "Total Due: $205.50"
]

var y: CGFloat = 720
for line in lines {
    context.showText(at: CGPoint(x: 72, y: y), string: line)
    y -= 24
}

context.endPDFPage()
context.closePDF()
