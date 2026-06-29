<?php
/**
 * EC_Rag_Utils_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Utils_Tests extends TestCase {

    public static function truthyProvider(): array {
        return [
            '1'    => ['1', true],
            'true' => ['true', true],
            'yes'  => ['yes', true],
            'on'   => ['on', true],
            '0'    => ['0', false],
            'false' => ['false', false],
            'no'   => ['no', false],
            'empty' => ['', false],
        ];
    }

    /** @dataProvider truthyProvider */
    public function test_is_truthy($value, bool $expected): void {
        self::assertSame($expected, \EC_Rag_Utils::is_truthy($value));
    }

    public function test_sanitize_response_language(): void {
        self::assertSame('auto', \EC_Rag_Utils::sanitize_response_language(''));
        self::assertSame('auto', \EC_Rag_Utils::sanitize_response_language('invalid'));
        self::assertSame('it', \EC_Rag_Utils::sanitize_response_language('it'));
        self::assertSame('en', \EC_Rag_Utils::sanitize_response_language('en'));
    }

    public function test_html_to_text_converts_html(): void {
        self::assertStringContainsString(
            'Hello world',
            \EC_Rag_Utils::html_to_text('<p>Hello <strong>world</strong></p>')
        );
    }

    public function test_html_to_text_empty(): void {
        self::assertSame('', \EC_Rag_Utils::html_to_text(''));
    }

    public function test_snapshot_content(): void {
        $result = \EC_Rag_Utils::snapshot_content([
            'post_id'      => 11,
            'title'        => 'Test Post',
            'link'         => 'https://example.com/test',
            'published_at' => '2024-01-01',
            'excerpt'      => 'An excerpt.',
            'content'      => 'Full content here.',
        ]);
        self::assertStringContainsString('Title: Test Post', $result);
        self::assertStringContainsString('Excerpt: An excerpt.', $result);
        self::assertStringContainsString('Full content here.', $result);
    }
}
