<?php
/**
 * EC_Rag_Api_Client_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Api_Client_Tests extends TestCase {

    protected function setUp(): void {
        $GLOBALS['ec_rag_test_filters']['ec_rag_api_retry_delay'] = function () {
            return 0;
        };
        $GLOBALS['ec_rag_test_http_handler'] = function () {
            return [
                'response' => ['code' => 200],
                'body'     => '{}',
                'headers'  => [],
            ];
        };
    }

    protected function tearDown(): void {
        unset($GLOBALS['ec_rag_test_filters']['ec_rag_api_retry_delay']);
    }

    private function client(): \EC_Rag_Api_Client {
        return new \EC_Rag_Api_Client(function () {
            return [
                'base_url'        => 'https://rag.example.test',
                'api_key'         => 'test-key',
                'request_timeout' => '5',
            ];
        });
    }

    public function test_post_decodes_successful_json_response(): void {
        $GLOBALS['ec_rag_test_http_handler'] = function ($method, $url, $args) {
            self::assertSame('POST', $method);
            self::assertSame('https://rag.example.test/api/v1/query', $url);
            self::assertSame('test-key', $args['headers']['X-API-Key']);
            self::assertJson($args['body']);

            return [
                'response' => ['code' => 200],
                'body'     => '{"answer":"ok"}',
                'headers'  => [],
            ];
        };

        self::assertSame(['answer' => 'ok'], $this->client()->post('/api/v1/query', ['query' => 'hello']));
    }

    public function test_retries_transient_server_errors(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function () use (&$attempts) {
            $attempts++;

            if ($attempts === 1) {
                return [
                    'response' => ['code' => 503],
                    'body'     => '{"message":"try later"}',
                    'headers'  => [],
                ];
            }

            return [
                'response' => ['code' => 200],
                'body'     => '{"status":"healthy"}',
                'headers'  => [],
            ];
        };

        self::assertSame(['status' => 'healthy'], $this->client()->get('/api/v1/health'));
        self::assertSame(2, $attempts);
    }

    public function test_does_not_retry_client_errors(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function () use (&$attempts) {
            $attempts++;

            return [
                'response' => ['code' => 403],
                'body'     => '{"message":"forbidden"}',
                'headers'  => [],
            ];
        };

        $result = $this->client()->get('/api/v1/health');

        self::assertInstanceOf(\WP_Error::class, $result);
        self::assertSame('forbidden', $result->get_error_message());
        self::assertSame(1, $attempts);
    }
}
