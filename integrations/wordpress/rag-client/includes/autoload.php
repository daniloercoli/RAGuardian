<?php
/**
 * Autoloader for EC_Rag classes.
 *
 * Naming convention: EC_Rag_FooBar  =>  includes/class-ec-rag-foo-bar.php
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

require_once __DIR__ . '/version.php';

spl_autoload_register(function ($class) {
    if (0 !== strpos($class, 'EC_Rag_')) {
        return;
    }

    $suffix = substr($class, 7);
    $filename = 'class-ec-rag-' . strtolower(str_replace('_', '-', $suffix)) . '.php';
    $file = __DIR__ . '/' . $filename;

    if (is_readable($file)) {
        require_once $file;
    }
});
