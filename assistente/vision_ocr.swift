import CoreGraphics
import Foundation
import ImageIO
import Vision

struct OCRRegion: Encodable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double

    var box: CGRect {
        CGRect(x: x, y: y, width: width, height: height)
    }
}

let maximumObservationsPerPass = 100
let maximumOutputRegions = 300

func makeScaledImage(_ image: CGImage, factor: Int) throws -> CGImage {
    let width = image.width * factor
    let height = image.height * factor
    guard width <= 4096, height <= 4096, width * height <= 12_000_000 else {
        throw NSError(domain: "CerebroVisionOCR", code: 2)
    }
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw NSError(domain: "CerebroVisionOCR", code: 3)
    }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let scaled = context.makeImage() else {
        throw NSError(domain: "CerebroVisionOCR", code: 4)
    }
    return scaled
}

func recognize(_ image: CGImage) throws -> [OCRRegion] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["pt-BR", "en-US"]
    request.usesLanguageCorrection = false

    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).prefix(maximumObservationsPerPass).flatMap { observation in
        let box = observation.boundingBox
        return observation.topCandidates(3).map { candidate in
            OCRRegion(
                text: candidate.string,
                confidence: candidate.confidence,
                x: box.origin.x,
                y: box.origin.y,
                width: box.width,
                height: box.height
            )
        }
    }
}

func overlapRatio(_ first: CGRect, _ second: CGRect) -> CGFloat {
    let intersection = first.intersection(second)
    guard !intersection.isNull, first.union(second).width > 0, first.union(second).height > 0 else {
        return 0
    }
    let intersectionArea = intersection.width * intersection.height
    let unionArea = first.width * first.height + second.width * second.height - intersectionArea
    return unionArea > 0 ? intersectionArea / unionArea : 0
}

func recognizeAcrossScales(_ image: CGImage) throws -> [OCRRegion] {
    var merged: [OCRRegion] = []
    for factor in [1, 2, 3] {
        let scaled = factor == 1 ? image : try makeScaledImage(image, factor: factor)
        for region in try recognize(scaled) {
            if let index = merged.firstIndex(where: {
                $0.text == region.text && overlapRatio($0.box, region.box) >= 0.5
            }) {
                if region.confidence > merged[index].confidence {
                    merged[index] = region
                }
            } else {
                guard merged.count < maximumOutputRegions else {
                    throw NSError(domain: "CerebroVisionOCR", code: 5)
                }
                merged.append(region)
            }
        }
    }
    return merged
}

do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty,
          let source = CGImageSourceCreateWithData(data as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "CerebroVisionOCR", code: 1)
    }
    let output = try JSONEncoder().encode(recognizeAcrossScales(image))
    FileHandle.standardOutput.write(output)
} catch {
    fputs("Apple Vision could not read the emulator screenshot.\n", stderr)
    exit(EXIT_FAILURE)
}
