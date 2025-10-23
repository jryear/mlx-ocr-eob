// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "vision-ocr-cli",
    platforms: [
        .macOS(.v15)
    ],
    targets: [
        .executableTarget(
            name: "vision-ocr-cli",
            linkerSettings: [
                .linkedFramework("Vision"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("ImageIO")
            ]
        )
    ]
)
