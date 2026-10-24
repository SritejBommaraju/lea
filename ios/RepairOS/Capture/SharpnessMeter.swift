// Adapted from ObjectScanner Engines/Turntable/SharpnessMeter.swift (Apache-2.0, Copyright 2026 Burak Sahinkaya),
// see THIRD_PARTY_NOTICES.md. Changed: added `laplacianVariance`, the value the contracts call `sharpness`.
import CoreVideo

/// Relative sharpness of a preview buffer.
///
/// Motion blur is the single most destructive input error in photogrammetry: a
/// soft frame does not merely contribute less, it contributes *wrong* feature
/// matches and can pull the whole alignment off. One bad frame is worse than a
/// missing angle, which is why blurred shots are dropped rather than kept.
///
/// The metric is gradient energy — mean squared difference between horizontally
/// adjacent pixels. It has no absolute meaning, only relative: the same scene shot
/// sharp scores several times higher than the same scene shot soft. That is enough
/// to compare frames within one session.
enum SharpnessMeter {

    static func score(_ buffer: CVPixelBuffer) -> Float? {
        let format = CVPixelBufferGetPixelFormatType(buffer)

        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        guard width > 8, height > 8 else { return nil }

        switch format {
        case kCVPixelFormatType_32BGRA:
            guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
            return gradientEnergy(
                base: base,
                bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
                width: width,
                height: height,
                bytesPerPixel: 4,
                // Green carries most of the luminance in a Bayer-derived image and
                // needs no colour conversion.
                channelOffset: 1
            )

        case kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
             kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange:
            // Plane 0 is luma — exactly what a sharpness metric wants.
            guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0) else { return nil }
            return gradientEnergy(
                base: base,
                bytesPerRow: CVPixelBufferGetBytesPerRowOfPlane(buffer, 0),
                width: CVPixelBufferGetWidthOfPlane(buffer, 0),
                height: CVPixelBufferGetHeightOfPlane(buffer, 0),
                bytesPerPixel: 1,
                channelOffset: 0
            )

        default:
            return nil
        }
    }

    private static func gradientEnergy(
        base: UnsafeMutableRawPointer,
        bytesPerRow: Int,
        width: Int,
        height: Int,
        bytesPerPixel: Int,
        channelOffset: Int
    ) -> Float {
        var total: Double = 0
        var samples = 0

        // Every other row is plenty for a relative score and halves the work.
        for row in stride(from: 0, to: height, by: 2) {
            let rowBase = base.advanced(by: row * bytesPerRow).assumingMemoryBound(to: UInt8.self)
            for column in 0..<(width - 1) {
                let left = Int(rowBase[column * bytesPerPixel + channelOffset])
                let right = Int(rowBase[(column + 1) * bytesPerPixel + channelOffset])
                let difference = Double(right - left)
                total += difference * difference
                samples += 1
            }
        }

        return samples > 0 ? Float(total / Double(samples)) : 0
    }
}

extension SharpnessMeter {
    /// Variance of the 3x3 Laplacian `[0 1 0; 1 -4 1; 0 1 0]` over the luma plane, sampled on a grid about
    /// 480 px wide so the score is comparable across stream and still resolutions. Higher is sharper.
    static func laplacianVariance(_ buffer: CVPixelBuffer) -> Double? {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        guard format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
            || format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange else { return nil }
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0) else { return nil }
        let width = CVPixelBufferGetWidthOfPlane(buffer, 0)
        let height = CVPixelBufferGetHeightOfPlane(buffer, 0)
        let rowBytes = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        let step = max(1, width / 480)
        guard width > 3 * step, height > 3 * step else { return nil }
        let px = base.assumingMemoryBound(to: UInt8.self)
        var sum = 0.0, sumSq = 0.0, n = 0.0
        var y = step
        while y < height - step {
            var x = step
            let row = y * rowBytes
            while x < width - step {
                let c = Double(px[row + x])
                let lap = Double(px[row - step * rowBytes + x]) + Double(px[row + step * rowBytes + x])
                    + Double(px[row + x - step]) + Double(px[row + x + step]) - 4 * c
                sum += lap
                sumSq += lap * lap
                n += 1
                x += step
            }
            y += step
        }
        guard n > 0 else { return nil }
        let mean = sum / n
        return max(0, sumSq / n - mean * mean)
    }
}
