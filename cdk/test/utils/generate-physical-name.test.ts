import { App, Stack } from "aws-cdk-lib";
import { generatePhysicalName } from "../../lib/utils/generate-physical-name";

/**
 * Test helper function: create a test stack
 */
function createTestStack() {
  const app = new App();
  return new Stack(app, "TestStack");
}

describe("generatePhysicalName", () => {
  // Spy on console.warn to check warning messages
  let consoleWarnSpy: jest.SpyInstance;

  beforeEach(() => {
    consoleWarnSpy = jest.spyOn(console, "warn").mockImplementation();
  });

  afterEach(() => {
    consoleWarnSpy.mockRestore();
  });

  test("should generate a name with default options", () => {
    // Given
    const stack = createTestStack();
    const prefix = "TestPrefix";

    // When
    const result = generatePhysicalName(stack, prefix);

    // Then
    expect(result).toMatch(/^TestPrefix[A-F0-9]+$/);
    expect(consoleWarnSpy).not.toHaveBeenCalled();
  });

  test("should generate a lowercase name when lower option is true", () => {
    // Given
    const stack = createTestStack();
    const prefix = "TestPrefix";

    // When
    const result = generatePhysicalName(stack, prefix, { lower: true });

    // Then
    expect(result).toMatch(/^testprefix[a-f0-9]+$/);
    expect(consoleWarnSpy).not.toHaveBeenCalled();
  });

  test("should include separator when provided", () => {
    // Given
    const stack = createTestStack();
    const prefix = "TestPrefix";

    // When
    const result = generatePhysicalName(stack, prefix, { separator: "-" });

    // Then
    // The actual format is prefix + optional hash from destroyCreate + separator + unique hash
    // Since we didn't provide destroyCreate, the format is prefix + separator + unique hash
    expect(result).toMatch(/^TestPrefix-[A-F0-9]+$/);
    expect(consoleWarnSpy).not.toHaveBeenCalled();
  });

  test("should truncate prefix when it exceeds maxLength", () => {
    // Given
    const stack = createTestStack();
    const longPrefix = "A".repeat(100);
    const maxLength = 50;

    // When
    const result = generatePhysicalName(stack, longPrefix, { 
      maxLength,
      suppressWarnings: true // Suppress warnings for this test
    });

    // Then
    expect(result.length).toBeLessThanOrEqual(maxLength);
  });

  test("should log warning when truncation occurs", () => {
    // Given
    const stack = createTestStack();
    const longPrefix = "A".repeat(100);
    const maxLength = 50;

    // When
    const result = generatePhysicalName(stack, longPrefix, { maxLength });

    // Then
    expect(result.length).toBeLessThanOrEqual(maxLength);
    expect(consoleWarnSpy).toHaveBeenCalled();
  });

  test("should not log warning when suppressWarnings is true", () => {
    // Given
    const stack = createTestStack();
    const longPrefix = "A".repeat(100);
    const maxLength = 50;

    // When
    const result = generatePhysicalName(stack, longPrefix, { 
      maxLength,
      suppressWarnings: true
    });

    // Then
    expect(result.length).toBeLessThanOrEqual(maxLength);
    expect(consoleWarnSpy).not.toHaveBeenCalled();
  });

  test("should include destroyCreate hash when provided", () => {
    // Given
    const stack = createTestStack();
    const prefix = "TestPrefix";
    const destroyCreate = { someValue: "test" };

    // When
    const result1 = generatePhysicalName(stack, prefix, { destroyCreate });
    const result2 = generatePhysicalName(stack, prefix, { destroyCreate: { someValue: "different" } });
    const result3 = generatePhysicalName(stack, prefix, { destroyCreate });

    // Then
    // Same destroyCreate object should produce same hash
    expect(result1).toEqual(result3);
    // Different destroyCreate object should produce different hash
    expect(result1).not.toEqual(result2);
  });

  test("should reserve minUniqueNameLength characters for uniqueness", () => {
    // Given
    const stack = createTestStack();
    const prefix = "TestPrefix";
    const minUniqueNameLength = 10;

    // When
    const result = generatePhysicalName(stack, prefix, { minUniqueNameLength });

    // Then
    // Extract the unique part (after the prefix)
    const uniquePart = result.substring(prefix.length);
    // The unique part should have at least minUniqueNameLength characters
    expect(uniquePart.length).toBeGreaterThanOrEqual(minUniqueNameLength);
  });

  test("should generate different names for different scopes", () => {
    // Given
    const app = new App();
    const stack1 = new Stack(app, "TestStack1");
    const stack2 = new Stack(app, "TestStack2");
    const prefix = "TestPrefix";

    // When
    const result1 = generatePhysicalName(stack1, prefix);
    const result2 = generatePhysicalName(stack2, prefix);

    // Then
    expect(result1).not.toEqual(result2);
  });

  test("should handle extreme truncation gracefully", () => {
    // Given
    const stack = createTestStack();
    const longPrefix = "A".repeat(1000);
    const maxLength = 20;

    // When
    const result = generatePhysicalName(stack, longPrefix, { 
      maxLength,
      suppressWarnings: true
    });

    // Then
    expect(result.length).toBeLessThanOrEqual(maxLength);
    // Should still have at least one character from the prefix
    expect(result.startsWith("A")).toBeTruthy();
  });
});
