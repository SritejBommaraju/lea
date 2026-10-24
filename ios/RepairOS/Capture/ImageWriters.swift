import CoreImage
import CoreVideo
import Foundation
import ImageIO
import UniformTypeIdentifiers

/// A value that crosses to a background queue without a Sendable proof (pixel buffers are read-only there).
struct Unchecked<T>: @unchecked Sendable { let value: T }

/// File writers for keyframes and stills. Images stay in the sensor's native (landscape) orientation so the
/// intrinsics ARKit reports apply to the JPEG as is; no EXIF location is ever written.
enum ImageWriters {
    private static let context = CIContext(options: [.cacheIntermediates: false])

    /// JPEG of a camera buffer, optionally downscaled. Returns the pixel size written.
    @discardableResult
    static func writeJPEG(_ buffer: CVPixelBuffer, to url: URL, quality: Double, scale: Double = 1) throws -> (Int, Int) {
        var image = CIImage(cvPixelBuffer: buffer)
        if scale != 1 {
            image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale), highQualityDownsample: true)
        }
        let extent = image.extent.integral
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let data = context.jpegRepresentation(
                of: image.cropped(to: extent), colorSpace: space,
                options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): quality])
        else { throw WriterError.encode(url.lastPathComponent) }
        try data.write(to: url, options: .atomic)
        return (Int(extent.width), Int(extent.height))
    }

    /// ARKit depth (Float32 metres) as a 16-bit greyscale PNG in millimetres (`depth_encoding = uint16_mm`, the
    /// Stray Scanner format). 0 = no depth.
    static func writeDepthPNG(_ depth: CVPixelBuffer, to url: URL) throws {
        CVPixelBufferLockBaseAddress(depth, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depth, .readOnly) }
        let w = CVPixelBufferGetWidth(depth), h = CVPixelBufferGetHeight(depth)
        let rowBytes = CVPixelBufferGetBytesPerRow(depth)
        guard let base = CVPixelBufferGetBaseAddress(depth) else { throw WriterError.encode(url.lastPathComponent) }
        var mm = [UInt16](repeating: 0, count: w * h)
        for y in 0..<h {
            let row = base.advanced(by: y * rowBytes).assumingMemoryBound(to: Float32.self)
            for x in 0..<w {
                let d = row[x]
                mm[y * w + x] = d.isFinite && d > 0 ? UInt16(min(65535, (d * 1000).rounded())) : 0
            }
        }
        // PNG stores 16-bit samples big-endian; CGImage takes host order with the byte-order flag.
        try mm.withUnsafeBytes { raw in
            try writePNG(bytes: raw, width: w, height: h, bitsPerComponent: 16, bytesPerRow: w * 2,
                         bitmapInfo: CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder16Little.rawValue), to: url)
        }
    }

    /// Depth already copied out of ARKit (metres, row-major) as the same 16-bit millimetre PNG.
    static func writeDepthPNG(_ depth: [Float], width: Int, height: Int, to url: URL) throws {
        let mm = depth.map { d -> UInt16 in d.isFinite && d > 0 ? UInt16(min(65535, (d * 1000).rounded())) : 0 }
        try mm.withUnsafeBytes { raw in
            try writePNG(bytes: raw, width: width, height: height, bitsPerComponent: 16, bytesPerRow: width * 2,
                         bitmapInfo: CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder16Little.rawValue), to: url)
        }
    }

    /// ARKit confidence map (UInt8 0/1/2) as an 8-bit greyscale PNG.
    static func writeConfidencePNG(_ conf: CVPixelBuffer, to url: URL) throws {
        CVPixelBufferLockBaseAddress(conf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(conf, .readOnly) }
        let w = CVPixelBufferGetWidth(conf), h = CVPixelBufferGetHeight(conf)
        let rowBytes = CVPixelBufferGetBytesPerRow(conf)
        guard let base = CVPixelBufferGetBaseAddress(conf) else { throw WriterError.encode(url.lastPathComponent) }
        try writePNG(bytes: UnsafeRawBufferPointer(start: base, count: rowBytes * h), width: w, height: h,
                     bitsPerComponent: 8, bytesPerRow: rowBytes, bitmapInfo: CGBitmapInfo(), to: url)
    }

    static func writePNG(bytes: UnsafeRawBufferPointer, width: Int, height: Int, bitsPerComponent: Int,
                         bytesPerRow: Int, bitmapInfo: CGBitmapInfo, to url: URL) throws {
        guard let provider = CGDataProvider(data: Data(bytes) as CFData),
              let image = CGImage(width: width, height: height, bitsPerComponent: bitsPerComponent,
                                  bitsPerPixel: bitsPerComponent, bytesPerRow: bytesPerRow,
                                  space: CGColorSpaceCreateDeviceGray(), bitmapInfo: bitmapInfo, provider: provider,
                                  decode: nil, shouldInterpolate: false, intent: .defaultIntent),
              let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)
        else { throw WriterError.encode(url.lastPathComponent) }
        CGImageDestinationAddImage(dest, image, nil)
        guard CGImageDestinationFinalize(dest) else { throw WriterError.encode(url.lastPathComponent) }
    }

    enum WriterError: LocalizedError {
        case encode(String)
        var errorDescription: String? {
            switch self { case .encode(let name): "Could not write \(name)." }
        }
    }
}
