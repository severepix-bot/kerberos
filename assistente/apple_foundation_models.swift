import Foundation
import FoundationModels

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(EXIT_FAILURE)
}

let promptData = FileHandle.standardInput.readDataToEndOfFile()
guard let prompt = String(data: promptData, encoding: .utf8),
      !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
    fail("A tarefa enviada ao Apple Intelligence está vazia ou inválida.")
}

guard case .available = SystemLanguageModel.default.availability else {
    fail("Apple Intelligence indisponível: \(SystemLanguageModel.default.availability)")
}

do {
    let session = LanguageModelSession()
    let response = try await session.respond(
        to: prompt,
        options: GenerationOptions(maximumResponseTokens: 1024)
    )
    print(response.content)
} catch {
    fail("Falha na geração local do Apple Intelligence: \(error.localizedDescription)")
}
