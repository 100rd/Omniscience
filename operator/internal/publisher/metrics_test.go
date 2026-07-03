// Tests that the publisher correctly increments operator metrics on its
// success and error branches (#165).
//
// The natsPublisher cannot be exercised without a NATS broker; instead these
// tests assert the classifyNATSError helper produces the correct closed-enum
// value for every NATS error shape we care about. The integration with the
// metric counters is asserted in metrics_test.go in the metrics package.
package publisher

import (
	"errors"
	"testing"

	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
)

// TestClassifyNATSError covers the closed enum mapping. A regression here
// would route an alert to the wrong category — e.g. an ack_timeout being
// counted as a connect error and missing the OmniscienceOperatorPublishErrorBurst
// alert that groups by error_type.
func TestClassifyNATSError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want string
	}{
		{
			name: "no responders",
			err:  errors.New("nats: no responders available for request"),
			want: opmetrics.PublisherErrorNATSNoResponders,
		},
		{
			name: "ack timeout",
			err:  errors.New("nats: timeout waiting for ack"),
			want: opmetrics.PublisherErrorAckTimeout,
		},
		{
			name: "connection refused",
			err:  errors.New("dial tcp: connection refused"),
			want: opmetrics.PublisherErrorConnect,
		},
		{
			name: "connect generic",
			err:  errors.New("nats: connect failed: handshake aborted"),
			want: opmetrics.PublisherErrorConnect,
		},
		{
			name: "unknown error falls back to publish",
			err:  errors.New("nats: unexpected error"),
			want: opmetrics.PublisherErrorPublish,
		},
		{
			name: "nil error falls back to publish",
			err:  nil,
			want: opmetrics.PublisherErrorPublish,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := classifyNATSError(tc.err)
			if got != tc.want {
				t.Errorf("classifyNATSError(%v) = %q, want %q", tc.err, got, tc.want)
			}
		})
	}
}
