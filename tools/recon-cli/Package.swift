// swift-tools-version:6.0
// recon-cli: a small macOS command-line wrapper around Apple's PhotogrammetrySession (RealityKit). No package
// dependencies; system frameworks only (RealityKit, ModelIO, CoreMotion, CoreVideo, ImageIO).
import PackageDescription

let package = Package(
    name: "recon-cli",
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(name: "recon-cli", path: "Sources/recon-cli"),
    ]
)
