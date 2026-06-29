<?php
/**
 * <Platform> Plugin for RAGuardian
 *
 * Bootstrap entry point.
 *
 * @package EC_Rag
 */

if (!defined('ABSPATH')) {
    exit;
}

// Load includes.
require_once __DIR__ . '/includes/version.php';
require_once __DIR__ . '/includes/autoload.php';

// Register components.
EC_Rag_Options::register();
EC_Rag_Widget::register();
EC_Rag_Ajax::register();
EC_Rag_Health_Check::register();
