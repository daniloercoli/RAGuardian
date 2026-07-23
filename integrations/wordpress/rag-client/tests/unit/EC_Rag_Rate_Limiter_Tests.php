<?php
/**
 * EC_Rag_Rate_Limiter_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Rate_Limiter_Tests extends TestCase {

    protected function setUp(): void {
        $GLOBALS['ec_rag_test_transients'] = [];
        $_SERVER['REMOTE_ADDR'] = '203.0.113.10';
        $_POST = [];
        $_COOKIE = [];
        $GLOBALS['ec_rag_test_user_id'] = 0;
    }

    public function test_limiter_class_exists(): void {
        self::assertTrue(class_exists(\EC_Rag_Rate_Limiter::class));
    }

    public function test_limiter_instantiation(): void {
        $limiter = new \EC_Rag_Rate_Limiter();
        self::assertInstanceOf(\EC_Rag_Rate_Limiter::class, $limiter);
    }

    public function test_limiter_allows_until_configured_limit(): void {
        $limiter = new \EC_Rag_Rate_Limiter();

        self::assertTrue($limiter->check('chat', 2, 60));
        self::assertTrue($limiter->check('chat', 2, 60));

        $third = $limiter->check('chat', 2, 60);
        self::assertInstanceOf(\WP_Error::class, $third);
        self::assertSame('ec_rag_rate_limited', $third->get_error_code());
    }

    public function test_limiter_separates_actions(): void {
        $limiter = new \EC_Rag_Rate_Limiter();

        self::assertTrue($limiter->check('chat', 1, 60));
        self::assertTrue($limiter->check('audio', 1, 60));
    }

    public function test_rotating_conversation_id_does_not_bypass_guest_limit(): void {
        $limiter = new \EC_Rag_Rate_Limiter();

        $_POST['conversation_id'] = 'conversation-one';
        self::assertTrue($limiter->check('chat', 1, 60));

        $_POST['conversation_id'] = 'conversation-two';
        self::assertInstanceOf(\WP_Error::class, $limiter->check('chat', 1, 60));
    }

    public function test_logged_in_users_have_separate_buckets(): void {
        $limiter = new \EC_Rag_Rate_Limiter();

        $GLOBALS['ec_rag_test_user_id'] = 11;
        self::assertTrue($limiter->check('chat', 1, 60));

        $GLOBALS['ec_rag_test_user_id'] = 22;
        self::assertTrue($limiter->check('chat', 1, 60));
    }
}
