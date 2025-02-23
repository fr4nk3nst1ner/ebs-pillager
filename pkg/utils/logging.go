package utils

import (
	"fmt"
	"io"
	"log"
	"os"
)

var (
	// InfoLogger for standard logging
	InfoLogger *log.Logger
	// DebugLogger for debug logging
	DebugLogger *log.Logger
	// ErrorLogger for error logging
	ErrorLogger *log.Logger
)

// InitLogging initializes logging with the specified format
func InitLogging(debug bool) error {
	InfoLogger = log.New(os.Stdout, "[INFO] ", log.Ldate|log.Ltime|log.LUTC)
	ErrorLogger = log.New(os.Stderr, "[ERROR] ", log.Ldate|log.Ltime|log.LUTC)

	if debug {
		DebugLogger = log.New(os.Stdout, "[DEBUG] ", log.Ldate|log.Ltime|log.LUTC|log.Lshortfile)
	} else {
		DebugLogger = log.New(io.Discard, "", 0)
	}

	// Set standard logger format for backward compatibility
	log.SetFlags(log.Ldate | log.Ltime | log.LUTC)
	log.SetPrefix("[ebs-pillage] ")
	
	return nil
}

// SetupFileLogging sets up logging to a file in addition to stdout
func SetupFileLogging(logFile string) error {
	if logFile == "" {
		return nil
	}

	f, err := os.OpenFile(logFile, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0666)
	if err != nil {
		return fmt.Errorf("failed to open log file: %w", err)
	}

	// Create multi-writer for both file and stdout/stderr
	infoWriter := io.MultiWriter(os.Stdout, f)
	errorWriter := io.MultiWriter(os.Stderr, f)
	debugWriter := io.MultiWriter(os.Stdout, f)

	InfoLogger.SetOutput(infoWriter)
	ErrorLogger.SetOutput(errorWriter)
	if DebugLogger.Writer() != io.Discard {
		DebugLogger.SetOutput(debugWriter)
	}

	return nil
}

// Debug logs a debug message if debug logging is enabled
func Debug(format string, v ...interface{}) {
	DebugLogger.Printf(format, v...)
}

// Info logs an info message
func Info(format string, v ...interface{}) {
	InfoLogger.Printf(format, v...)
}

// Error logs an error message
func Error(format string, v ...interface{}) {
	ErrorLogger.Printf(format, v...)
} 